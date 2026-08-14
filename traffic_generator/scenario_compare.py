"""Switch cluster state and run the three TrafficGenerator comparison cases."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import load_config
from .experiment_cli import (
    estimate_alias_demands,
    planned_alias_count_for_demands,
    run_experiment,
    service_alias_count_overrides,
    timestamp_run_id,
)
from .ksvc_aliases import (
    KsvcAliasConfig,
    build_generate_ksvc_command,
    load_ksvc_alias_config,
    with_ksvc_alias_count,
)
from .metrics_aggregate import aggregate_metrics
from .metrics_config import MetricsConfig, load_metrics_config
from .metrics_plots import plot_metrics
from .metrics_runs import run_plots_dir
from .models import ReplayConfig, ServiceConfig
from .service_map import ServiceResolver
from .trace_loader import load_trace

DEFAULT_CASES = ("static-knative", "fixed-boost", "full-nimbus")
NIMBUS_SELECTOR_KEY = "serving.knative.dev/service"
USER_CONTAINER = "user-container"
QUEUE_PROXY_REQUEST_CPU_CORES = 0.025


@dataclass(frozen=True)
class ServiceScenarioPlan:
    service: ServiceConfig
    namespace: str
    alias_count: int
    aliases: tuple[str, ...]
    node_selector: dict[str, str]

    @property
    def service_base(self) -> str:
        return self.service.service_base


@dataclass(frozen=True)
class CommandRunner:
    dry_run: bool = False
    verbose: bool = False

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self.verbose or self.dry_run:
            rendered = " ".join(command)
            print(f"+ {rendered}")
            if input_text is not None and self.verbose:
                print(input_text.rstrip())
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, "", "")
        completed = subprocess.run(
            list(command),
            check=False,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
        )
        if check and completed.returncode != 0:
            if capture_output:
                if completed.stdout:
                    print(completed.stdout, end="")
                if completed.stderr:
                    print(completed.stderr, end="", file=sys.stderr)
            raise subprocess.CalledProcessError(
                completed.returncode,
                completed.args,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return completed


def run_compare(args: argparse.Namespace) -> int:
    central_config = args.config or "trafficgen.config.toml"
    replay_config = load_config(central_config)
    metrics_config = load_metrics_config(central_config)
    rows = load_trace(replay_config.trace.file)  # type: ignore[arg-type]
    assumed_service_time_sec = _assumed_service_time_sec(args, replay_config)
    demands = estimate_alias_demands(
        rows,
        replay_config,
        assumed_service_time_sec=assumed_service_time_sec,
        service_resolver=ServiceResolver.from_config(replay_config),
    )
    alias_overrides = service_alias_count_overrides(replay_config)
    global_alias_count = planned_alias_count_for_demands(demands, alias_overrides)
    plans = build_service_plans(replay_config, demands, alias_overrides)
    if not plans:
        raise ValueError("no scheduled traffic matched configured [[services]]")
    if len(plans) > 1:
        raise ValueError(
            "experiment compare currently supports one active [[services]] block; "
            "alias preparation is still single-service"
        )

    run_id = args.run_id or timestamp_run_id()
    cases = tuple(args.cases or DEFAULT_CASES)
    _validate_cases(cases)
    runner = CommandRunner(dry_run=args.dry_run, verbose=args.verbose)

    _print_compare_plan(run_id, cases, plans, global_alias_count, args.dry_run)
    sys.stdout.flush()
    if not args.skip_cluster_capacity_check:
        capacity_runner = CommandRunner(dry_run=False, verbose=args.verbose)
        try:
            validate_node_selectors(plans, capacity_runner)
            validate_selected_nodes_ready(plans, capacity_runner)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            print_compare_capacity(plans, cases, capacity_runner)
        except RuntimeError as exc:
            print(f"warning: compare capacity check skipped: {exc}", file=sys.stderr)
    if not args.dry_run and not args.yes and not _confirm_compare():
        print("aborted before applying cluster state or running traffic")
        return 1

    if not args.skip_crd_apply:
        apply_required_crds(replay_config, runner)

    if not args.skip_alias_prepare:
        prepare_aliases(central_config, plans[0], runner)

    returncode = 0
    stop_after_case = False
    for case in cases:
        try:
            try:
                apply_case_state(
                    case,
                    replay_config,
                    metrics_config,
                    plans,
                    runner,
                    skip_nimbus_service=args.skip_nimbus_service,
                    wait_timeout_sec=args.state_wait_timeout_sec,
                )
                if args.dry_run:
                    traffic_code = _run_case_dry_plan(args, central_config, case, run_id)
                else:
                    traffic_code = run_experiment(
                        _case_run_args(args, central_config, case, run_id)
                    )
                if traffic_code != 0:
                    returncode = returncode or traffic_code
                    stop_after_case = not args.continue_on_failure
            except KeyboardInterrupt:
                print(
                    f"warning: comparison interrupted during {case}; cleaning case CRs",
                    file=sys.stderr,
                )
                returncode = 130
                stop_after_case = True
            except (subprocess.CalledProcessError, TimeoutError, RuntimeError) as exc:
                print(f"error: comparison case {case} failed: {exc}", file=sys.stderr)
                returncode = returncode or 1
                stop_after_case = not args.continue_on_failure
        finally:
            cleanup_case_crs(replay_config.target.namespace, runner)
        if stop_after_case:
            return returncode

    if args.dry_run:
        print("dry-run complete; no metrics were aggregated or plotted")
        return returncode

    try:
        csv_paths = aggregate_metrics(metrics_config, run_id=run_id)
        _print_paths("wrote comparison CSVs", csv_paths.values())
        comparison_metrics_dir = Path(csv_paths["summary_metrics"]).parent
    except KeyboardInterrupt:
        print("warning: comparison aggregation interrupted; plots were skipped", file=sys.stderr)
        return 130
    try:
        plot_paths = plot_metrics(
            metrics_config,
            input_dir=comparison_metrics_dir,
            output_dir=run_plots_dir(metrics_config, run_id),
            run_id=run_id,
        )
        _print_paths("wrote comparison plots", plot_paths.values())
    except KeyboardInterrupt:
        print("warning: comparison plotting interrupted; comparison CSVs were written", file=sys.stderr)
        return 130
    return returncode


def build_service_plans(
    config: ReplayConfig,
    demands,
    alias_overrides: Mapping[str, int],
) -> list[ServiceScenarioPlan]:
    demand_by_service = {demand.service_base: demand for demand in demands}
    plans: list[ServiceScenarioPlan] = []
    for service in config.services:
        demand = demand_by_service.get(service.service_base)
        if demand is None:
            continue
        alias_count = alias_overrides.get(service.service_base, demand.required_aliases)
        if alias_count < 1:
            continue
        plans.append(
            ServiceScenarioPlan(
                service=service,
                namespace=config.target.namespace,
                alias_count=alias_count,
                aliases=tuple(
                    format_alias_name(
                        config.routing.suffix_template,
                        service.service_base,
                        index,
                    )
                    for index in range(1, alias_count + 1)
                ),
                node_selector=parse_node_selector(service.nimbus.node_pool_selector),
            )
        )
    return plans


def validate_node_selectors(plans: Sequence[ServiceScenarioPlan], runner: CommandRunner) -> None:
    for plan in plans:
        if not plan.node_selector:
            continue
        selector = ",".join(f"{key}={value}" for key, value in sorted(plan.node_selector.items()))
        completed = runner.run(
            ["kubectl", "get", "nodes", "-l", selector, "--no-headers"],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "kubectl get nodes failed").strip()
            raise RuntimeError(f"cannot validate node selector {selector!r}: {detail}")
        if pods_list_has_rows(completed.stdout or ""):
            continue
        raise RuntimeError(
            f"no nodes match selector {selector!r} for service {plan.service_base}; "
            f"label the intended worker node first, for example: "
            f"kubectl label node <node-name> {selector} --overwrite"
        )


def validate_selected_nodes_ready(
    plans: Sequence[ServiceScenarioPlan],
    runner: CommandRunner,
) -> None:
    nodes = runner_kubectl_json(["kubectl", "get", "nodes", "-o", "json"], runner)
    for plan in plans:
        if not plan.node_selector:
            continue
        selected = nodes_matching_selector(nodes, plan.node_selector)
        ready = [node for node in selected if node_is_ready_schedulable(node)]
        if ready:
            continue
        selector = ",".join(
            f"{key}={value}" for key, value in sorted(plan.node_selector.items())
        )
        detail = "; ".join(node_unavailable_reason(node) for node in selected)
        if not detail:
            detail = "no matching nodes found"
        raise RuntimeError(
            f"no Ready schedulable nodes match selector {selector!r} "
            f"for service {plan.service_base}: {detail}"
        )


def node_is_ready_schedulable(node: Mapping[str, Any]) -> bool:
    if node.get("spec", {}).get("unschedulable"):
        return False
    status = node_condition_status(node, "Ready")
    if status != "True":
        return False
    for taint in node.get("spec", {}).get("taints", []) or []:
        if not isinstance(taint, Mapping):
            continue
        if taint.get("effect") in {"NoSchedule", "NoExecute"}:
            return False
    return True


def node_unavailable_reason(node: Mapping[str, Any]) -> str:
    name = str(node.get("metadata", {}).get("name", "<unknown>"))
    reasons: list[str] = []
    ready = node_condition_status(node, "Ready") or "Unknown"
    if ready != "True":
        reasons.append(f"Ready={ready}")
    if node.get("spec", {}).get("unschedulable"):
        reasons.append("unschedulable=true")
    taints = []
    for taint in node.get("spec", {}).get("taints", []) or []:
        if isinstance(taint, Mapping):
            key = taint.get("key")
            effect = taint.get("effect")
            if key and effect:
                taints.append(f"{key}:{effect}")
    if taints:
        reasons.append("taints=" + ",".join(taints))
    return f"{name} (" + "; ".join(reasons or ["not schedulable"]) + ")"


def node_condition_status(node: Mapping[str, Any], condition_type: str) -> str | None:
    for condition in node.get("status", {}).get("conditions", []) or []:
        if isinstance(condition, Mapping) and condition.get("type") == condition_type:
            raw = condition.get("status")
            return str(raw) if raw is not None else None
    return None


def print_compare_capacity(
    plans: Sequence[ServiceScenarioPlan],
    cases: Sequence[str],
    runner: CommandRunner,
) -> None:
    nodes = runner_kubectl_json(["kubectl", "get", "nodes", "-o", "json"], runner)
    pods = runner_kubectl_json(["kubectl", "get", "pods", "-A", "-o", "json"], runner)
    print("Compare capacity")
    for plan in plans:
        selected_nodes = nodes_matching_selector(nodes, plan.node_selector)
        if not selected_nodes:
            continue
        node_names = tuple(
            str(node.get("metadata", {}).get("name", ""))
            for node in selected_nodes
            if node.get("metadata", {}).get("name")
        )
        allocatable = selected_nodes_allocatable_cpu(selected_nodes)
        requested = selected_nodes_requested_cpu(pods, node_names)
        available = max(0.0, allocatable - requested)
        node_text = ",".join(node_names) if node_names else "<unknown>"
        print(
            f"  {plan.service_base}: nodes={node_text} "
            f"available_cpu={format_cores(available)} aliases={plan.alias_count}"
        )
        for label, per_alias in case_cpu_requirements(plan.service, cases):
            required = plan.alias_count * per_alias
            if required <= available:
                print(
                    f"  ok {label}: {plan.alias_count} aliases x "
                    f"{format_cores(per_alias)} cores = {format_cores(required)} cores"
                )
                continue
            max_aliases = int(available // per_alias) if per_alias > 0 else 0
            print(
                f"  warning {label}: {plan.alias_count} aliases x "
                f"{format_cores(per_alias)} cores = {format_cores(required)} cores; "
                f"max on selected nodes ~= {max_aliases}"
            )


def runner_kubectl_json(command: Sequence[str], runner: CommandRunner) -> Mapping[str, Any]:
    completed = runner.run(command, check=False, capture_output=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "kubectl command failed").strip()
        raise RuntimeError(detail)
    if not completed.stdout:
        raise RuntimeError("kubectl returned no JSON")
    loaded = json.loads(completed.stdout)
    if not isinstance(loaded, Mapping):
        raise RuntimeError("kubectl JSON response was not an object")
    return loaded


def nodes_matching_selector(
    nodes: Mapping[str, Any],
    selector: Mapping[str, str],
) -> tuple[Mapping[str, Any], ...]:
    matched = []
    for node in nodes.get("items", []):
        if not isinstance(node, Mapping):
            continue
        labels = node.get("metadata", {}).get("labels", {})
        if not isinstance(labels, Mapping):
            labels = {}
        if all(labels.get(key) == value for key, value in selector.items()):
            matched.append(node)
    return tuple(matched)


def selected_nodes_allocatable_cpu(nodes: Sequence[Mapping[str, Any]]) -> float:
    total = 0.0
    for node in nodes:
        cpu = node.get("status", {}).get("allocatable", {}).get("cpu")
        total += parse_cpu_quantity(cpu) or 0.0
    return total


def selected_nodes_requested_cpu(pods: Mapping[str, Any], node_names: Sequence[str]) -> float:
    selected = set(node_names)
    total = 0.0
    for pod in pods.get("items", []):
        if not isinstance(pod, Mapping):
            continue
        status = pod.get("status", {})
        if isinstance(status, Mapping) and status.get("phase") in {"Succeeded", "Failed"}:
            continue
        spec = pod.get("spec", {})
        if not isinstance(spec, Mapping) or spec.get("nodeName") not in selected:
            continue
        containers = list(spec.get("containers", []) or [])
        containers.extend(spec.get("initContainers", []) or [])
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            cpu = container.get("resources", {}).get("requests", {}).get("cpu")
            total += parse_cpu_quantity(cpu) or 0.0
    return total


def case_cpu_requirements(
    service: ServiceConfig,
    cases: Sequence[str],
) -> tuple[tuple[str, float], ...]:
    requirements: list[tuple[str, float]] = []
    selected_cases = set(cases)
    if "static-knative" in selected_cases:
        add_case_cpu_requirement(requirements, "static-knative", service.cpu.cpu_budget_static)
    if "fixed-boost" in selected_cases:
        add_case_cpu_requirement(requirements, "fixed-boost warm", service.cpu.c_opt_warm)
        add_case_cpu_requirement(requirements, "fixed-boost cold-start", service.cpu.c_opt_cold)
    if "full-nimbus" in selected_cases:
        add_case_cpu_requirement(requirements, "full-nimbus budget", service.cpu.cpu_budget_static)
    return tuple(requirements)


def add_case_cpu_requirement(
    requirements: list[tuple[str, float]],
    label: str,
    cpu: str | None,
) -> None:
    parsed = parse_cpu_quantity(cpu)
    if parsed is None:
        return
    requirements.append((label, parsed + QUEUE_PROXY_REQUEST_CPU_CORES))


def parse_cpu_quantity(value: Any) -> float | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        if text.endswith("m"):
            return float(text[:-1]) / 1000
        if text.endswith("n"):
            return float(text[:-1]) / 1_000_000_000
        if text.endswith("u"):
            return float(text[:-1]) / 1_000_000
        return float(text)
    except ValueError:
        return None


def format_cores(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def apply_required_crds(config: ReplayConfig, runner: CommandRunner) -> None:
    kscb_repo = config.system.repos.kscb
    nimbus_repo = config.system.repos.nimbus
    if kscb_repo is not None:
        kscb_crd = kscb_repo / "config" / "crd" / "bases" / "autoscaling.x-k8s.io_startupcpuboosts.yaml"
        if kscb_crd.exists():
            runner.run(["kubectl", "apply", "-f", str(kscb_crd)])
        else:
            raise FileNotFoundError(f"KSCB CRD manifest not found: {kscb_crd}")
    if nimbus_repo is not None:
        nimbus_crd = nimbus_repo / "config" / "crd.yaml"
        if nimbus_crd.exists():
            runner.run(["kubectl", "apply", "-f", str(nimbus_crd)])
        else:
            raise FileNotFoundError(f"Nimbus CRD manifest not found: {nimbus_crd}")


def prepare_aliases(
    central_config: str,
    plan: ServiceScenarioPlan,
    runner: CommandRunner,
) -> None:
    alias_config = with_ksvc_alias_count(load_ksvc_alias_config(central_config), plan.alias_count)
    alias_config = safe_alias_config_for_compare(alias_config)
    if not alias_config.enabled:
        print("alias preparation disabled; assuming aliases already exist")
        return
    print_alias_prepare_plan(alias_config, plan)
    if runner.dry_run:
        runner.run(build_generate_ksvc_command(alias_config))
        return
    runner.run(build_generate_ksvc_command(alias_config))


def safe_alias_config_for_compare(config: KsvcAliasConfig) -> KsvcAliasConfig:
    if not config.enabled or not config.apply:
        return config
    return replace(config, no_wait_ready=False, no_wait_scale_zero=False)


def print_alias_prepare_plan(config: KsvcAliasConfig, plan: ServiceScenarioPlan) -> None:
    if not config.apply:
        print(f"alias preparation: generate {plan.alias_count} manifests; apply is disabled")
        return
    print(
        "alias preparation: sequential apply; existing aliases with matching image "
        "are reused, and each alias waits scale-to-zero before the next one"
    )
    print(
        f"  order: {plan.aliases[0]}..{plan.aliases[-1]} "
        f"({plan.alias_count} aliases)"
    )


def apply_case_state(
    case: str,
    config: ReplayConfig,
    metrics_config: MetricsConfig,
    plans: Sequence[ServiceScenarioPlan],
    runner: CommandRunner,
    *,
    skip_nimbus_service: bool,
    wait_timeout_sec: float,
) -> None:
    namespace = config.target.namespace
    print(f"switching cluster state: {case}")
    ensure_namespace(namespace, runner)
    delete_case_crs(namespace, runner)

    if case == "static-knative":
        for plan in plans:
            require_cpu(plan.service, "cpu_budget_static")
            apply_static_alias_state(
                namespace,
                plan,
                plan.service.cpu.cpu_budget_static or "",
                runner,
                wait_timeout_sec,
            )
        return

    if case == "fixed-boost":
        for plan in plans:
            require_cpu(plan.service, "c_opt_warm")
            require_cpu(plan.service, "c_opt_cold")
            apply_fixed_boost_alias_state(
                namespace,
                plan,
                warm_cpu=plan.service.cpu.c_opt_warm or "",
                cold_cpu=plan.service.cpu.c_opt_cold or "",
                runner=runner,
                wait_timeout_sec=wait_timeout_sec,
            )
        return

    if case == "full-nimbus":
        if not skip_nimbus_service:
            apply_nimbus_service(config, runner)
        for plan in plans:
            require_cpu(plan.service, "cpu_budget_static")
            require_pre_measured(plan.service)
            apply_nimbus_cr(namespace, metrics_config, plan, runner)
        if not runner.dry_run:
            wait_for_nimbus_apply(namespace, plans, runner, wait_timeout_sec)
        wait_for_alias_ready(
            [alias for plan in plans for alias in plan.aliases],
            namespace,
            runner,
            wait_timeout_sec,
        )
        for plan in plans:
            delete_alias_pods(namespace, plan.aliases, runner)
        wait_for_scale_zero(plans, namespace, runner, wait_timeout_sec)
        return

    raise AssertionError(f"unknown case: {case}")


def ensure_namespace(namespace: str, runner: CommandRunner) -> None:
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}
    apply_manifest(manifest, runner)


def apply_static_alias_state(
    namespace: str,
    plan: ServiceScenarioPlan,
    cpu: str,
    runner: CommandRunner,
    wait_timeout_sec: float,
) -> None:
    for alias in plan.aliases:
        changed = patch_one_alias_ksvc(plan, alias, cpu, runner)
        wait_for_alias_ready([alias], namespace, runner, wait_timeout_sec, force=changed)
        delete_alias_pods(namespace, [alias], runner)
        wait_for_scale_zero_aliases(namespace, [alias], runner, wait_timeout_sec)


def apply_fixed_boost_alias_state(
    namespace: str,
    plan: ServiceScenarioPlan,
    *,
    warm_cpu: str,
    cold_cpu: str,
    runner: CommandRunner,
    wait_timeout_sec: float,
) -> None:
    for alias in plan.aliases:
        apply_manifest(build_fixed_boost_cr(namespace, alias, cold_cpu), runner)
        changed = patch_one_alias_ksvc(plan, alias, warm_cpu, runner)
        wait_for_alias_ready([alias], namespace, runner, wait_timeout_sec, force=changed)
        delete_alias_pods(namespace, [alias], runner)
        wait_for_scale_zero_aliases(namespace, [alias], runner, wait_timeout_sec)


def delete_case_crs(namespace: str, runner: CommandRunner) -> None:
    runner.run(["kubectl", "delete", "nimbus", "-n", namespace, "--all", "--ignore-not-found"], check=False)
    runner.run(["kubectl", "delete", "startupcpuboost", "-n", namespace, "--all", "--ignore-not-found"], check=False)


def cleanup_case_crs(namespace: str, runner: CommandRunner) -> None:
    print(f"cleaning case CRs in namespace {namespace}: nimbus, startupcpuboost")
    delete_case_crs(namespace, runner)


def patch_alias_ksvcs(plan: ServiceScenarioPlan, cpu: str, runner: CommandRunner) -> tuple[str, ...]:
    changed_aliases: list[str] = []
    for alias in plan.aliases:
        if patch_one_alias_ksvc(plan, alias, cpu, runner):
            changed_aliases.append(alias)
    return tuple(changed_aliases)


def patch_one_alias_ksvc(
    plan: ServiceScenarioPlan,
    alias: str,
    cpu: str,
    runner: CommandRunner,
) -> bool:
    patch = json.dumps(build_ksvc_patch(cpu, plan.node_selector), separators=(",", ":"))
    completed = runner.run(
        [
            "kubectl",
            "patch",
            "ksvc",
            alias,
            "-n",
            plan.namespace,
            "--type=json",
            "-p",
            patch,
        ],
        capture_output=True,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if "(no change)" not in output:
        return True
    if ksvc_needs_revision_retry(alias, plan.namespace, runner):
        force_ksvc_revision_retry(alias, plan.namespace, runner)
        return True
    return False


def ksvc_needs_revision_retry(alias: str, namespace: str, runner: CommandRunner) -> bool:
    completed = runner.run(
        ["kubectl", "get", "ksvc", alias, "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout:
        return False
    loaded = json.loads(completed.stdout)
    status = loaded.get("status", {})
    ready_status = ksvc_condition_status(status, "Ready")
    latest_created = status.get("latestCreatedRevisionName")
    latest_ready = status.get("latestReadyRevisionName")
    return bool(ready_status == "False" and latest_created and latest_created != latest_ready)


def force_ksvc_revision_retry(alias: str, namespace: str, runner: CommandRunner) -> None:
    retry_value = str(int(time.time()))
    patch = json.dumps(
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "trafficgenerator.io/retry-revision": retry_value,
                        }
                    }
                }
            }
        },
        separators=(",", ":"),
    )
    print(
        f"warning: ksvc/{alias} latest revision failed; forcing retry revision {retry_value}"
    )
    runner.run(
        [
            "kubectl",
            "patch",
            "ksvc",
            alias,
            "-n",
            namespace,
            "--type=merge",
            "-p",
            patch,
        ]
    )


def build_ksvc_patch(cpu: str, node_selector: Mapping[str, str]) -> list[dict[str, Any]]:
    patch: list[dict[str, Any]] = [
        {
            "op": "add",
            "path": "/spec/template/spec/containers/0/resources",
            "value": {"requests": {"cpu": cpu}, "limits": {"cpu": cpu}},
        },
        {
            "op": "add",
            "path": "/spec/template/metadata/annotations/autoscaling.knative.dev~1max-scale",
            "value": "1",
        },
        {
            "op": "add",
            "path": "/spec/template/metadata/annotations/autoscaling.knative.dev~1min-scale",
            "value": "0",
        },
    ]
    if node_selector:
        patch.append(
            {
                "op": "add",
                "path": "/spec/template/spec/nodeSelector",
                "value": dict(node_selector),
            }
        )
    return patch


def apply_fixed_boost_crs(
    namespace: str,
    plan: ServiceScenarioPlan,
    cold_cpu: str,
    runner: CommandRunner,
) -> None:
    for alias in plan.aliases:
        apply_manifest(build_fixed_boost_cr(namespace, alias, cold_cpu), runner)


def build_fixed_boost_cr(namespace: str, alias: str, cold_cpu: str) -> dict[str, Any]:
    return {
        "apiVersion": "autoscaling.x-k8s.io/v1alpha1",
        "kind": "StartupCPUBoost",
        "metadata": {
            "name": f"fixed-boost-{alias}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "TrafficGenerator",
                "trafficgenerator.io/configuration": "fixed-boost",
                "trafficgenerator.io/ksvc": alias,
            },
        },
        "selector": {
            "matchExpressions": [
                {"key": NIMBUS_SELECTOR_KEY, "operator": "In", "values": [alias]}
            ]
        },
        "spec": {
            "resourcePolicy": {
                "containerPolicies": [
                    {
                        "containerName": USER_CONTAINER,
                        "fixedResources": {"requests": cold_cpu, "limits": cold_cpu},
                    }
                ]
            },
            "durationPolicy": {
                "apiCondition": {
                    "url": f"http://{alias}.{namespace}.svc.cluster.local/status",
                    "response": "READY",
                }
            },
        },
    }


def apply_nimbus_service(config: ReplayConfig, runner: CommandRunner) -> None:
    nimbus_repo = config.system.repos.nimbus
    if nimbus_repo is None:
        raise ValueError("system.repos.nimbus is required for full-nimbus")
    manifest = nimbus_repo / "config" / "nimbus-service.yaml"
    if not manifest.exists():
        raise FileNotFoundError(f"Nimbus Service manifest not found: {manifest}")
    runner.run(["kubectl", "apply", "-f", str(manifest)])


def apply_nimbus_cr(
    namespace: str,
    metrics_config: MetricsConfig,
    plan: ServiceScenarioPlan,
    runner: CommandRunner,
) -> None:
    apply_manifest(build_nimbus_cr(namespace, metrics_config, plan), runner)


def build_nimbus_cr(
    namespace: str,
    metrics_config: MetricsConfig,
    plan: ServiceScenarioPlan,
) -> dict[str, Any]:
    service = plan.service
    path = service.path or "/detect/local"
    pre_measured_dir = service.nimbus.pre_measured_dir
    return {
        "apiVersion": "lazyken.io/v1alpha1",
        "kind": "Nimbus",
        "metadata": {"name": nimbus_name(service.service_base), "namespace": namespace},
        "selector": {
            "matchExpressions": [
                {"key": NIMBUS_SELECTOR_KEY, "operator": "In", "values": list(plan.aliases)}
            ]
        },
        "spec": {
            "placement": {"nodeSelector": dict(plan.node_selector)},
            "metric": "p95",
            "acceptableResponseTime": {
                "cold": int(metrics_config.cold_slo_ms),
                "warm": int(metrics_config.warm_slo_ms),
            },
            "preMeasured": {"loadFromDir": str(pre_measured_dir)},
            "online": {"enabled": True},
            "resourcePolicy": {
                "containerPolicies": [
                    {
                        "containerName": USER_CONTAINER,
                        "cpuBudget": service.cpu.cpu_budget_static,
                    }
                ]
            },
            "durationPolicy": {
                "coldApiCondition": {"path": "/status", "response": "READY"},
                "warmApiCondition": {
                    "path": path,
                    "statusCode": 200,
                    "bodyContains": '"success":true',
                },
            },
        },
    }


def apply_manifest(manifest: Mapping[str, Any], runner: CommandRunner) -> None:
    runner.run(
        ["kubectl", "apply", "-f", "-"],
        input_text=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
    )


def wait_for_aliases(
    plans: Sequence[ServiceScenarioPlan],
    namespace: str,
    runner: CommandRunner,
    timeout_sec: float,
    *,
    ready_aliases: Sequence[str] | None = None,
) -> None:
    aliases_requiring_ready = set(ready_aliases) if ready_aliases is not None else None
    for plan in plans:
        for alias in plan.aliases:
            force = aliases_requiring_ready is None or alias in aliases_requiring_ready
            wait_for_alias_ready([alias], namespace, runner, timeout_sec, force=force)
    wait_for_scale_zero(plans, namespace, runner, timeout_sec)


def wait_for_alias_ready(
    aliases: Sequence[str],
    namespace: str,
    runner: CommandRunner,
    timeout_sec: float,
    *,
    force: bool = True,
) -> None:
    if runner.dry_run:
        return
    for alias in aliases:
        if not force and ksvc_has_ready_revision(alias, namespace, runner):
            continue
        runner.run([
            "kubectl",
            "wait",
            "-n",
            namespace,
            f"ksvc/{alias}",
            "--for=condition=Ready",
            f"--timeout={int(timeout_sec)}s",
        ])


def ksvc_has_ready_revision(alias: str, namespace: str, runner: CommandRunner) -> bool:
    completed = runner.run(
        ["kubectl", "get", "ksvc", alias, "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout:
        return False
    loaded = json.loads(completed.stdout)
    status = loaded.get("status", {})
    ready_status = ksvc_condition_status(status, "Ready")
    if ready_status == "True":
        return True
    latest_created = status.get("latestCreatedRevisionName")
    latest_ready = status.get("latestReadyRevisionName")
    if latest_ready and latest_ready == latest_created:
        print(
            f"warning: ksvc/{alias} Ready is {ready_status or 'unknown'}; "
            f"reusing latest ready revision {latest_ready}"
        )
        return True
    return False


def ksvc_condition_status(status: Mapping[str, Any], condition_type: str) -> str | None:
    conditions = status.get("conditions", [])
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if isinstance(condition, Mapping) and condition.get("type") == condition_type:
            raw_status = condition.get("status")
            return str(raw_status) if raw_status is not None else None
    return None


def wait_for_scale_zero(
    plans: Sequence[ServiceScenarioPlan],
    namespace: str,
    runner: CommandRunner,
    timeout_sec: float,
) -> None:
    wait_for_scale_zero_aliases(
        namespace,
        [alias for plan in plans for alias in plan.aliases],
        runner,
        timeout_sec,
    )


def wait_for_scale_zero_aliases(
    namespace: str,
    aliases: Sequence[str],
    runner: CommandRunner,
    timeout_sec: float,
) -> None:
    if runner.dry_run:
        return
    deadline = time.monotonic() + timeout_sec
    while True:
        active = []
        for alias in aliases:
            completed = runner.run(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    namespace,
                    "-l",
                    f"serving.knative.dev/service={alias}",
                    "--no-headers",
                ],
                check=False,
                capture_output=True,
            )
            if pods_list_has_rows(completed.stdout or ""):
                active.append(alias)
        if not active:
            return
        if time.monotonic() >= deadline:
            active_text = ", ".join(active)
            raise TimeoutError(f"timed out waiting for aliases to scale to zero: {active_text}")
        time.sleep(2)


def wait_for_nimbus_apply(
    namespace: str,
    plans: Sequence[ServiceScenarioPlan],
    runner: CommandRunner,
    timeout_sec: float,
) -> None:
    deadline = time.monotonic() + timeout_sec
    expected = {plan.service_base: set(plan.aliases) for plan in plans}
    while True:
        missing: dict[str, set[str]] = {}
        errors: list[str] = []
        for plan in plans:
            completed = runner.run(
                [
                    "kubectl",
                    "get",
                    "nimbus",
                    nimbus_name(plan.service_base),
                    "-n",
                    namespace,
                    "-o",
                    "json",
                ],
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0 or not completed.stdout:
                missing[plan.service_base] = expected[plan.service_base]
                continue
            loaded = json.loads(completed.stdout)
            applied = loaded.get("status", {}).get("applied", {})
            missing_aliases = expected[plan.service_base] - set(applied)
            if missing_aliases:
                missing[plan.service_base] = missing_aliases
            for alias, row in applied.items():
                if isinstance(row, Mapping) and row.get("applyError"):
                    errors.append(f"{alias}: {row['applyError']}")
        if errors:
            raise RuntimeError("Nimbus apply errors: " + "; ".join(errors))
        if not missing:
            return
        if time.monotonic() >= deadline:
            detail = ", ".join(f"{svc}={sorted(values)}" for svc, values in missing.items())
            raise TimeoutError(f"timed out waiting for Nimbus apply status: {detail}")
        time.sleep(2)


def delete_alias_pods(namespace: str, aliases: Sequence[str], runner: CommandRunner) -> None:
    for alias in aliases:
        runner.run(
            [
                "kubectl",
                "delete",
                "pod",
                "-n",
                namespace,
                "-l",
                f"serving.knative.dev/service={alias}",
                "--ignore-not-found",
            ],
            check=False,
        )


def pods_list_has_rows(output: str) -> bool:
    stripped = output.strip()
    return bool(stripped and not stripped.startswith("No resources found"))


def parse_node_selector(value: str | None) -> dict[str, str]:
    if value is None or not value.strip():
        return {}
    result: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"invalid node selector {value!r}; expected key=value")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise ValueError(f"invalid node selector {value!r}; expected key=value")
        result[key] = raw_value
    return result


def format_alias_name(suffix_template: str, service_base: str, index: int) -> str:
    return suffix_template.format(service_base=service_base, base=service_base, index=index)


def nimbus_name(service_base: str) -> str:
    return f"nimbus-{service_base}"


def require_cpu(service: ServiceConfig, field: str) -> None:
    value = getattr(service.cpu, field)
    if value is None or not value.strip():
        raise ValueError(f"services[{service.service_base}].cpu.{field} is required")


def require_pre_measured(service: ServiceConfig) -> None:
    if service.nimbus.pre_measured_dir is None:
        raise ValueError(f"services[{service.service_base}].nimbus.pre_measured_dir is required")
    if not parse_node_selector(service.nimbus.node_pool_selector):
        raise ValueError(f"services[{service.service_base}].nimbus.node_pool_selector is required")


def _assumed_service_time_sec(args: argparse.Namespace, config: ReplayConfig) -> float:
    value = args.assumed_service_time_sec
    if value is None:
        value = config.routing.dry_run_assumed_service_time_sec
    if value is None:
        value = config.routing.request_timeout_sec
    if value <= 0:
        raise ValueError("assumed service time must be > 0")
    return float(value)


def _case_run_args(
    args: argparse.Namespace,
    central_config: str,
    case: str,
    run_id: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        config=central_config,
        configuration=case,
        run_id=run_id,
        users=args.users,
        spawn_rate=args.spawn_rate,
        timeout_sec=args.timeout_sec,
        assumed_service_time_sec=args.assumed_service_time_sec,
        yes=True,
        verbose=args.verbose,
        no_color=args.no_color,
        skip_alias_prepare=True,
        skip_cluster_capacity_check=args.skip_cluster_capacity_check or args.dry_run,
        skip_plot=args.skip_per_run_plot,
        dry_run=False,
    )


def _run_case_dry_plan(
    args: argparse.Namespace,
    central_config: str,
    case: str,
    run_id: str,
) -> int:
    dry_args = _case_run_args(args, central_config, case, run_id)
    dry_args.dry_run = True
    return run_experiment(dry_args)


def _validate_cases(cases: Sequence[str]) -> None:
    allowed = set(DEFAULT_CASES)
    unknown = [case for case in cases if case not in allowed]
    if unknown:
        raise ValueError(f"unknown comparison case(s): {', '.join(unknown)}")


def _confirm_compare() -> bool:
    if not sys.stdin.isatty():
        print("confirmation required; rerun with --yes to proceed non-interactively", file=sys.stderr)
        return False
    answer = input("Switch cluster state and run all comparison cases? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _print_compare_plan(
    run_id: str,
    cases: Sequence[str],
    plans: Sequence[ServiceScenarioPlan],
    global_alias_count: int,
    dry_run: bool,
) -> None:
    mode = "dry-run" if dry_run else "run"
    print(f"TrafficGenerator compare ({mode})")
    print(f"  run_id: {run_id}")
    print(f"  cases: {', '.join(cases)}")
    print(f"  planned_alias_count: {global_alias_count}")
    for plan in plans:
        alias_range = f"{plan.aliases[0]}..{plan.aliases[-1]}" if plan.aliases else "<none>"
        print(f"  service: {plan.service_base} aliases={alias_range} selector={plan.node_selector}")


def _print_paths(message: str, paths) -> None:
    normalized = [Path(path) for path in paths]
    if not normalized:
        print(f"{message}: none")
        return
    print(f"{message}:")
    for path in normalized:
        print(f"  {path}")
