"""One-command experiment runner for traffic replay and metrics output."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import load_config
from .ksvc_aliases import (
    KsvcAliasConfig,
    build_generate_ksvc_command,
    load_ksvc_alias_config,
    with_ksvc_alias_count,
)
from .metrics_aggregate import aggregate_metrics
from .metrics_config import load_metrics_config
from .metrics_plots import plot_metrics
from .metrics_prometheus import collect_prometheus_samples
from .metrics_runs import case_run_dir
from .models import ReplayConfig, TraceRow
from .poisson import SECONDS_PER_TRACE_MINUTE, scale_count
from .replay import selected_rows
from .service_map import ServiceResolver
from .trace_loader import load_trace


@dataclass(frozen=True)
class AliasDemand:
    service_base: str
    selected_minutes: int
    scheduled_requests: int
    max_original_rpm: int
    max_scaled_rpm: int
    peak_scaled_rps: float
    assumed_service_time_sec: float
    required_aliases: int


@dataclass(frozen=True)
class ClusterCapacityCheck:
    status: str
    message: str
    cpu_per_alias_cores: float | None = None
    required_cpu_cores: float | None = None
    allocatable_cpu_cores: float | None = None
    requested_cpu_cores: float | None = None
    available_cpu_cores: float | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run traffic and build metrics/plots")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Prepare aliases, run Locust, collect metrics, and plot")
    run.add_argument(
        "--config",
        default=None,
        help="Central trafficgen.config.toml containing replay, metrics, and alias settings",
    )
    run.add_argument(
        "--configuration",
        required=True,
        help="Result folder slug, for example static-knative, fixed-boost, or full-nimbus",
    )
    run.add_argument(
        "--run-id",
        help="Run folder name. Defaults to a timestamp such as 20260729-153012.",
    )
    run.add_argument("--users", "-u", type=int, default=1, help="Locust user count")
    run.add_argument("--spawn-rate", "-r", type=float, default=1.0, help="Locust spawn rate")
    run.add_argument("--timeout-sec", type=float, default=30.0, help="Prometheus HTTP timeout")
    run.add_argument(
        "--assumed-service-time-sec",
        type=float,
        help=(
            "Seconds each request is assumed to occupy one alias for clone-count "
            "calculation. Defaults to routing.dry_run_assumed_service_time_sec, "
            "then routing.request_timeout_sec."
        ),
    )
    run.add_argument("--yes", "-y", action="store_true", help="Run without the confirmation prompt")
    run.add_argument("--verbose", action="store_true", help="Print detailed preflight and commands")
    run.add_argument("--no-color", action="store_true", help="Disable colored terminal output")
    run.add_argument("--skip-alias-prepare", action="store_true", help="Do not run generateKsvc even if ksvc_aliases.enabled = true")
    run.add_argument("--skip-cluster-capacity-check", action="store_true", help="Do not query kubectl for a best-effort CPU capacity check")
    run.add_argument("--skip-plot", action="store_true", help="Collect and aggregate, but do not plot")
    run.add_argument("--dry-run", action="store_true", help="Print the run plan without applying aliases or running Locust")

    compare = subparsers.add_parser(
        "compare",
        help="Switch cluster state, run static/fixed/full cases, aggregate, and plot",
    )
    compare.add_argument(
        "--config",
        default=None,
        help="Central trafficgen.config.toml containing replay, metrics, and alias settings",
    )
    compare.add_argument("--run-id", help="Shared run folder name for all cases")
    compare.add_argument(
        "--cases",
        nargs="+",
        default=["static-knative", "fixed-boost", "full-nimbus"],
        help="Cases to run in order; default: static-knative fixed-boost full-nimbus",
    )
    compare.add_argument("--users", "-u", type=int, default=1, help="Locust user count")
    compare.add_argument("--spawn-rate", "-r", type=float, default=1.0, help="Locust spawn rate")
    compare.add_argument("--timeout-sec", type=float, default=30.0, help="Prometheus HTTP timeout")
    compare.add_argument(
        "--assumed-service-time-sec",
        type=float,
        help="Seconds each request is assumed to occupy one alias for clone-count calculation",
    )
    compare.add_argument("--yes", "-y", action="store_true", help="Run without the confirmation prompt")
    compare.add_argument("--verbose", action="store_true", help="Print kubectl and traffic commands")
    compare.add_argument("--no-color", action="store_true", help="Disable colored terminal output")
    compare.add_argument("--dry-run", action="store_true", help="Print state changes and run plans without applying or sending traffic")
    compare.add_argument("--skip-alias-prepare", action="store_true", help="Assume aliases already exist")
    compare.add_argument("--skip-crd-apply", action="store_true", help="Do not apply Nimbus/KSCB CRDs before switching cases")
    compare.add_argument("--skip-nimbus-service", action="store_true", help="Do not apply nimbus/config/nimbus-service.yaml for full-nimbus")
    compare.add_argument("--skip-cluster-capacity-check", action="store_true", help="Do not query kubectl for the per-run capacity preflight")
    compare.add_argument("--skip-per-run-plot", action="store_true", help="Skip per-case plots; final comparison plots are still written")
    compare.add_argument("--continue-on-failure", action="store_true", help="Continue remaining cases after a traffic failure")
    compare.add_argument("--state-wait-timeout-sec", type=float, default=300.0, help="Timeout for KService scale-to-zero and Nimbus apply waits")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return run_experiment(args)
    if args.command == "compare":
        from .scenario_compare import run_compare

        return run_compare(args)
    raise AssertionError(f"unknown experiment command: {args.command}")


def run_experiment(args: argparse.Namespace) -> int:
    central_config = args.config or "trafficgen.config.toml"

    replay_config = load_config(central_config)
    rows = load_trace(replay_config.trace.file)  # type: ignore[arg-type]
    metrics_config = load_metrics_config(central_config)
    alias_config = load_ksvc_alias_config(central_config)
    assumed_service_time_sec = _assumed_service_time_sec(args, replay_config)
    demands = estimate_alias_demands(
        rows,
        replay_config,
        assumed_service_time_sec=assumed_service_time_sec,
        service_resolver=ServiceResolver.from_config(replay_config),
    )
    computed_alias_count = max((demand.required_aliases for demand in demands), default=0)
    alias_overrides = service_alias_count_overrides(replay_config)
    planned_alias_count = planned_alias_count_for_demands(demands, alias_overrides)

    if alias_config.enabled and planned_alias_count > 0:
        alias_config = with_ksvc_alias_count(alias_config, planned_alias_count)

    alias_command = (
        []
        if args.skip_alias_prepare or not alias_config.enabled or planned_alias_count < 1
        else build_generate_ksvc_command(alias_config)
    )
    cluster_capacity = check_cluster_capacity(
        alias_config,
        planned_alias_count,
        skip=args.skip_cluster_capacity_check or planned_alias_count < 1,
    )
    run_id = args.run_id or timestamp_run_id()
    run_dir = case_run_dir(metrics_config, args.configuration, run_id).resolve()

    locust_command = build_locust_command(
        trafficgen_config=central_config,
        users=args.users,
        spawn_rate=args.spawn_rate,
    )
    env = os.environ.copy()
    env["LOG_DIR"] = str(run_dir)
    if replay_config.routing.increase_service and planned_alias_count > 0:
        env["MAX_KSVC_ALIASES"] = str(planned_alias_count)

    _print_run_plan(
        args,
        replay_config,
        configuration=args.configuration,
        run_id=run_id,
        run_dir=run_dir,
        demands=demands,
        computed_alias_count=computed_alias_count,
        planned_alias_count=planned_alias_count,
        alias_overrides=alias_overrides,
        alias_config=alias_config,
        alias_command=alias_command,
        cluster_capacity=cluster_capacity,
        locust_command=locust_command,
        runtime_alias_limit=planned_alias_count if replay_config.routing.increase_service else 0,
    )

    if args.dry_run:
        return 0
    if not _confirm_run(args.yes):
        print("aborted before applying aliases or running traffic")
        return 1

    if alias_command:
        completed = subprocess.run(alias_command, check=False)  # noqa: S603 - fixed executable/args.
        if completed.returncode != 0:
            return completed.returncode

    run_dir.mkdir(parents=True, exist_ok=True)
    traffic_returncode = 0
    try:
        completed = subprocess.run(locust_command, env=env, check=False)  # noqa: S603 - fixed executable/args.
        traffic_returncode = completed.returncode
    except KeyboardInterrupt:
        traffic_returncode = 130
        _print_warning("traffic run interrupted; writing metrics and plots from partial logs")

    if traffic_returncode != 0:
        _print_warning(
            f"traffic run exited with code {traffic_returncode}; "
            "writing metrics and plots from available logs"
        )

    output_returncode = _write_post_traffic_outputs(
        args,
        metrics_config,
        run_dir=run_dir,
        run_id=run_id,
    )
    return traffic_returncode or output_returncode


def service_alias_count_overrides(config: ReplayConfig) -> dict[str, int]:
    return {
        service.service_base: service.alias_count
        for service in config.services
        if service.alias_count is not None
    }


def planned_alias_count_for_demands(
    demands: Sequence[AliasDemand],
    alias_overrides: Mapping[str, int],
) -> int:
    if not demands:
        return 0
    return max(
        alias_overrides.get(demand.service_base, demand.required_aliases)
        for demand in demands
    )


def estimate_alias_demands(
    rows: Sequence[TraceRow],
    config: ReplayConfig,
    *,
    assumed_service_time_sec: float,
    service_resolver: ServiceResolver | None = None,
) -> list[AliasDemand]:
    if assumed_service_time_sec <= 0:
        raise ValueError("assumed_service_time_sec must be > 0")

    selected = selected_rows(rows, config)
    resolver = service_resolver or ServiceResolver.from_config(config)
    original_by_service_minute: dict[tuple[str, int], int] = {}
    scaled_by_service_minute: dict[tuple[str, int], int] = {}

    for row in selected:
        service_base = resolver.resolve(row.function_id)
        key = (service_base, row.minute)
        original_by_service_minute[key] = original_by_service_minute.get(key, 0) + row.count
        scaled_by_service_minute[key] = scaled_by_service_minute.get(key, 0) + scale_count(
            row.count,
            config.traffic.scale,
        )

    service_bases = sorted({service_base for service_base, _ in original_by_service_minute})
    demands: list[AliasDemand] = []
    for service_base in service_bases:
        original_values = [
            count
            for (candidate, _), count in original_by_service_minute.items()
            if candidate == service_base
        ]
        scaled_values = [
            count
            for (candidate, _), count in scaled_by_service_minute.items()
            if candidate == service_base
        ]
        scheduled_requests = sum(scaled_values)
        max_scaled_rpm = max(scaled_values, default=0)
        peak_scaled_rps = max_scaled_rpm / SECONDS_PER_TRACE_MINUTE
        required_aliases = (
            max(1, math.ceil(peak_scaled_rps * assumed_service_time_sec))
            if scheduled_requests > 0 and config.routing.increase_service
            else 0
        )
        demands.append(
            AliasDemand(
                service_base=service_base,
                selected_minutes=len({minute for candidate, minute in original_by_service_minute if candidate == service_base}),
                scheduled_requests=scheduled_requests,
                max_original_rpm=max(original_values, default=0),
                max_scaled_rpm=max_scaled_rpm,
                peak_scaled_rps=peak_scaled_rps,
                assumed_service_time_sec=assumed_service_time_sec,
                required_aliases=required_aliases,
            )
        )
    return demands


def check_cluster_capacity(
    alias_config: KsvcAliasConfig,
    alias_count: int,
    *,
    skip: bool = False,
) -> ClusterCapacityCheck:
    if skip:
        return ClusterCapacityCheck("skipped", "cluster capacity check skipped")
    if not alias_config.template:
        return ClusterCapacityCheck("unknown", "cluster capacity unknown: no KService template configured")

    cpu_per_alias = _template_cpu_request_cores(alias_config.template)
    if cpu_per_alias is None:
        return ClusterCapacityCheck(
            "unknown",
            "cluster CPU capacity unknown: template has no containers[].resources.requests.cpu",
        )

    nodes, node_error = _kubectl_json(["get", "nodes"])
    pods, pod_error = _kubectl_json(["get", "pods", "-A"])
    if nodes is None or pods is None:
        detail = node_error or pod_error or "kubectl did not return JSON"
        return ClusterCapacityCheck("unknown", f"cluster CPU capacity unknown: {detail}")

    allocatable_cpu = _node_allocatable_cpu_cores(nodes)
    requested_cpu = _pod_requested_cpu_cores(pods)
    available_cpu = max(0.0, allocatable_cpu - requested_cpu)
    required_cpu = alias_count * cpu_per_alias
    status = "ok" if required_cpu <= available_cpu else "exceeded"
    message = (
        "estimated alias CPU fits current allocatable headroom"
        if status == "ok"
        else "estimated alias CPU exceeds current allocatable headroom"
    )
    return ClusterCapacityCheck(
        status,
        message,
        cpu_per_alias_cores=cpu_per_alias,
        required_cpu_cores=required_cpu,
        allocatable_cpu_cores=allocatable_cpu,
        requested_cpu_cores=requested_cpu,
        available_cpu_cores=available_cpu,
    )


def timestamp_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def build_locust_command(
    *,
    trafficgen_config: str,
    users: int,
    spawn_rate: float,
) -> list[str]:
    locust = _locust_executable()
    locustfile = Path(__file__).resolve().parent / "locustfile.py"
    return [
        locust,
        "-f",
        str(locustfile),
        "--headless",
        "-u",
        str(users),
        "-r",
        _format_number(spawn_rate),
        "--trafficgen-config",
        str(Path(trafficgen_config).resolve()),
    ]


def _assumed_service_time_sec(args: argparse.Namespace, config: ReplayConfig) -> float:
    configured = config.routing.dry_run_assumed_service_time_sec
    value = args.assumed_service_time_sec if args.assumed_service_time_sec is not None else configured
    if value is None:
        value = config.routing.request_timeout_sec
    if value <= 0:
        raise ValueError("assumed service time must be > 0")
    return float(value)


def _print_run_plan(
    args: argparse.Namespace,
    config: ReplayConfig,
    *,
    configuration: str,
    run_id: str,
    run_dir: Path,
    demands: Sequence[AliasDemand],
    computed_alias_count: int,
    planned_alias_count: int,
    alias_overrides: Mapping[str, int],
    alias_config: KsvcAliasConfig,
    alias_command: Sequence[str],
    cluster_capacity: ClusterCapacityCheck,
    locust_command: Sequence[str],
    runtime_alias_limit: int,
) -> None:
    for warning in config.warnings:
        print(_style(f"warning: {warning}", Colors.YELLOW), file=sys.stderr)

    color = _color_enabled(args)
    print(_style("TrafficGenerator preflight", Colors.BOLD, color=color))
    _print_kv("Run", f"{configuration}/{run_id}", color=color)
    if args.verbose:
        _print_kv("Output", str(run_dir), color=color)
        _print_kv("Trace", f"{config.trace.file} minutes {config.trace.start_minute}..{config.trace.end_minute}", color=color)
        _print_kv("Scale", str(config.traffic.scale), color=color)

    print(_style("KServices", Colors.BOLD, color=color))
    if planned_alias_count > 0:
        range_text = _alias_range(alias_config, planned_alias_count)
        if range_text:
            _print_status("create", f"{planned_alias_count} aliases ({range_text})", color=color)
        else:
            _print_status("create", f"{planned_alias_count} aliases", color=color)
        if planned_alias_count != computed_alias_count:
            _print_status(
                "override",
                f"using {planned_alias_count} aliases; calculator estimated {computed_alias_count}",
                color=color,
            )
    else:
        _print_status("none", "suffix routing disabled or no scheduled traffic", color=color)

    for demand in demands:
        override = alias_overrides.get(demand.service_base)
        if override is None:
            demand_text = (
                f"{demand.service_base}: peak {_format_number(demand.peak_scaled_rps)} rps "
                f"x {_format_number(demand.assumed_service_time_sec)}s -> "
                f"{demand.required_aliases} aliases"
            )
        else:
            note = ""
            if override < demand.required_aliases:
                note = "; below computed demand, so routing may wait/reuse warm aliases"
            demand_text = (
                f"{demand.service_base}: peak {_format_number(demand.peak_scaled_rps)} rps "
                f"x {_format_number(demand.assumed_service_time_sec)}s -> "
                f"computed {demand.required_aliases}, configured {override}{note}"
            )
        _print_detail(
            "demand",
            demand_text,
            color=color,
        )
        _print_detail(
            "planning",
            (
                f"assumes each request keeps one alias busy for "
                f"{_format_number(demand.assumed_service_time_sec)}s "
                f"(request timeout {_format_number(config.routing.request_timeout_sec)}s)"
            ),
            color=color,
        )
        if args.verbose:
            _print_detail(
                "trace",
                (
                    f"{demand.selected_minutes} minutes, {demand.scheduled_requests} requests, "
                    f"max {demand.max_original_rpm} rpm -> {demand.max_scaled_rpm} scaled rpm"
                ),
                color=color,
            )

    print(_style("Capacity", Colors.BOLD, color=color))
    _print_capacity(cluster_capacity, color=color)

    print(_style("Action", Colors.BOLD, color=color))
    if args.skip_alias_prepare:
        _print_status("skip", "alias preparation disabled by --skip-alias-prepare", color=color)
    elif not alias_config.enabled:
        _print_status("manual", "aliases must already exist; [ksvc_aliases].enabled is false", color=color)
    elif alias_command:
        action = "apply" if alias_config.apply else "generate"
        _print_status(action, f"{planned_alias_count} aliases, then run traffic", color=color)
    else:
        _print_status("run", "traffic only", color=color)
    if runtime_alias_limit > 0:
        _print_status(
            "limit",
            f"Locust waits inside 001..{runtime_alias_limit:03d}; no out-of-range KService calls",
            color=color,
        )

    if args.verbose:
        if alias_command:
            print(_style("Commands", Colors.BOLD, color=color))
            _print_detail("ksvc", " ".join(alias_command), color=color)
            _print_detail("locust", " ".join(locust_command), color=color)
        else:
            print(_style("Commands", Colors.BOLD, color=color))
            _print_detail("locust", " ".join(locust_command), color=color)


def _write_post_traffic_outputs(
    args: argparse.Namespace,
    metrics_config,
    *,
    run_dir: Path,
    run_id: str,
) -> int:
    returncode = 0
    try:
        sample_paths = collect_prometheus_samples(
            metrics_config,
            run_dir=run_dir,
            timeout_sec=args.timeout_sec,
        )
        _print_paths("wrote Prometheus samples", sample_paths)
    except KeyboardInterrupt:
        _print_warning("Prometheus collection interrupted; stopping output phase")
        return 130
    except Exception as exc:  # noqa: BLE001 - keep partial-run output moving.
        returncode = 1
        _print_warning(f"Prometheus collection failed; continuing with log-only metrics: {exc}")

    try:
        csv_paths = aggregate_metrics(metrics_config, run_dir=run_dir)
        _print_paths("wrote aggregate CSVs", csv_paths.values())
    except KeyboardInterrupt:
        _print_warning("metrics aggregation interrupted; plots were skipped")
        return 130
    except Exception as exc:  # noqa: BLE001 - report why plots cannot be produced.
        _print_warning(f"metrics aggregation failed; plots were skipped: {exc}")
        return 1

    if args.skip_plot:
        return returncode

    try:
        plot_paths = plot_metrics(
            metrics_config,
            input_dir=run_dir,
            output_dir=run_dir,
            run_id=run_id,
        )
        _print_paths("wrote plots", plot_paths.values())
    except KeyboardInterrupt:
        _print_warning("plotting interrupted; Prometheus samples and aggregate CSVs were written")
        return 130
    except Exception as exc:  # noqa: BLE001 - surface plotting failures without hiding traffic failures.
        _print_warning(f"plotting failed: {exc}")
        returncode = 1
    return returncode


def _print_warning(message: str) -> None:
    print(_style(f"warning: {message}", Colors.YELLOW), file=sys.stderr)


def _print_capacity(check: ClusterCapacityCheck, *, color: bool) -> None:
    status_color = {
        "ok": Colors.GREEN,
        "exceeded": Colors.RED,
        "unknown": Colors.YELLOW,
        "skipped": Colors.DIM,
    }.get(check.status, Colors.RESET)
    label = _style(check.status, status_color, color=color)
    print(f"  {label}  {check.message}")
    if check.cpu_per_alias_cores is None:
        return
    print(
        "  "
        f"required={_format_number(check.required_cpu_cores or 0)} cores, "
        f"available={_format_number(check.available_cpu_cores or 0)} cores, "
        f"per_alias={_format_number(check.cpu_per_alias_cores)} cores"
    )


def _alias_range(alias_config: KsvcAliasConfig, computed_alias_count: int) -> str:
    if not alias_config.base_name or computed_alias_count < 1:
        return ""
    return f"{alias_config.base_name}-001..{alias_config.base_name}-{computed_alias_count:03d}"


def _print_kv(key: str, value: str, *, color: bool) -> None:
    print(f"  {_style(key + ':', Colors.CYAN, color=color)} {value}")


def _print_status(status: str, value: str, *, color: bool) -> None:
    print(f"  {_style(status, Colors.CYAN, color=color)}  {value}")


def _print_detail(label: str, value: str, *, color: bool) -> None:
    print(f"  {_style(label + ':', Colors.DIM, color=color)} {value}")


class Colors:
    BOLD = "\033[1m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"


def _color_enabled(args: argparse.Namespace) -> bool:
    return not args.no_color and "NO_COLOR" not in os.environ and sys.stdout.isatty()


def _style(text: str, color_code: str, *, color: bool | None = None) -> str:
    enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ if color is None else color
    if not enabled or not color_code:
        return text
    return f"{color_code}{text}{Colors.RESET}"

def _confirm_run(yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print("confirmation required; rerun with --yes to proceed non-interactively", file=sys.stderr)
        return False
    answer = input("Continue with alias preparation and traffic run? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _locust_executable() -> str:
    sibling = Path(sys.executable).with_name("locust")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("locust")
    if found:
        return found
    raise FileNotFoundError("locust executable not found")


def _template_cpu_request_cores(path: Path) -> float | None:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return None

    if not path.exists():
        return None
    loaded = [doc for doc in yaml.safe_load_all(path.read_text()) if isinstance(doc, dict)]
    service_doc = next(
        (
            doc
            for doc in loaded
            if doc.get("apiVersion") == "serving.knative.dev/v1"
            and doc.get("kind") == "Service"
        ),
        None,
    )
    if service_doc is None:
        return None

    total = 0.0
    found = False
    for container in _template_containers(service_doc):
        cpu = (
            container.get("resources", {})
            .get("requests", {})
            .get("cpu")
        )
        parsed = _parse_cpu_quantity(cpu)
        if parsed is not None:
            total += parsed
            found = True
    return total if found else None


def _template_containers(service_doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers = (
        service_doc.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if not isinstance(containers, list):
        return []
    return [container for container in containers if isinstance(container, Mapping)]


def _kubectl_json(args: Sequence[str]) -> tuple[dict[str, Any] | None, str | None]:
    command = ["kubectl", *args, "-o", "json"]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "kubectl not found"
    if completed.returncode != 0:
        return None, completed.stderr.strip() or "kubectl command failed"
    try:
        loaded = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return None, f"invalid kubectl JSON: {exc}"
    return loaded if isinstance(loaded, dict) else None, None


def _node_allocatable_cpu_cores(nodes: Mapping[str, Any]) -> float:
    total = 0.0
    for node in nodes.get("items", []):
        if not isinstance(node, Mapping):
            continue
        cpu = (
            node.get("status", {})
            .get("allocatable", {})
            .get("cpu")
        )
        total += _parse_cpu_quantity(cpu) or 0.0
    return total


def _pod_requested_cpu_cores(pods: Mapping[str, Any]) -> float:
    total = 0.0
    for pod in pods.get("items", []):
        if not isinstance(pod, Mapping):
            continue
        phase = str(pod.get("status", {}).get("phase", ""))
        if phase in {"Succeeded", "Failed"}:
            continue
        spec = pod.get("spec", {})
        containers = []
        if isinstance(spec, Mapping):
            containers.extend(spec.get("containers", []) or [])
            containers.extend(spec.get("initContainers", []) or [])
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            cpu = (
                container.get("resources", {})
                .get("requests", {})
                .get("cpu")
            )
            total += _parse_cpu_quantity(cpu) or 0.0
    return total


def _parse_cpu_quantity(value: Any) -> float | None:
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


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _print_paths(message: str, paths) -> None:
    normalized = [Path(path) for path in paths]
    if not normalized:
        print(f"{message}: none")
        return
    print(f"{message}:")
    for path in normalized:
        print(f"  {path}")
