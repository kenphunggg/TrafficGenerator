#!/usr/bin/env python3
"""Generate and apply suffixed Knative Services from one template."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SUFFIX_TEMPLATE = "{base}-{index:03d}"
DEFAULT_POLL_INTERVAL_SEC = 2.0
DEFAULT_READY_TIMEOUT_SEC = 300.0
DEFAULT_SCALE_ZERO_TIMEOUT_SEC = 300.0
COLOR_ENABLED = True


class Colors:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    RESET = "\033[0m"


def configure_color(args: argparse.Namespace) -> None:
    global COLOR_ENABLED
    COLOR_ENABLED = not args.no_color and "NO_COLOR" not in os.environ


def colored(text: str, color: str) -> str:
    if not COLOR_ENABLED:
        return text
    return f"{color}{text}{Colors.RESET}"


def print_kv(key: str, value: object) -> None:
    print(f"{colored(key + '=', Colors.CYAN)}{value}")


def print_step(message: str) -> None:
    print(colored(message, Colors.BLUE))


def print_success(message: str) -> None:
    print(colored(message, Colors.GREEN))


def print_warning(message: str) -> None:
    print(colored(f"warning: {message}", Colors.YELLOW), file=sys.stderr)


def print_error(message: str) -> None:
    print(colored(f"error: {message}", Colors.RED), file=sys.stderr)


def print_command(command: str) -> None:
    print(colored("+ " + command, Colors.DIM))


class KsvcGenerationError(ValueError):
    """Raised when the template or user inputs cannot be rendered safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate suffixed Knative Service aliases from a template."
    )
    parser.add_argument("--template", required=True, help="Path to Knative Service YAML template")
    parser.add_argument("--count", required=True, type=int, help="Number of ksvc aliases to create")
    parser.add_argument(
        "--base-name",
        help="Base service name. Defaults to metadata.name from the Knative Service template",
    )
    parser.add_argument(
        "--suffix-template",
        default=DEFAULT_SUFFIX_TEMPLATE,
        help='Python format string using {base} and {index}; default: "{base}-{index:03d}"',
    )
    parser.add_argument(
        "--output-dir",
        default="generated-ksvc",
        help="Directory where generated YAML files are written",
    )
    parser.add_argument("--nodes", help="JSON file containing nodes for image pre-pull")
    parser.add_argument(
        "--pull-images",
        action="store_true",
        help="SSH to every node and run crictl pull for each template image",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply generated ksvc YAML files with kubectl",
    )
    parser.add_argument(
        "--create-namespace",
        action="store_true",
        help="Create the target namespace before applying if it does not exist",
    )
    parser.add_argument(
        "--namespace",
        help="Override metadata.namespace; omitted means preserve the template namespace",
    )
    parser.add_argument("--kube-context", help="kubectl context to use")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port for image pre-pull")
    parser.add_argument("--ssh-key", help="SSH private key path for image pre-pull")
    parser.add_argument(
        "--crictl-command",
        default="sudo -n crictl pull",
        help='Remote image pull command prefix; default: "sudo -n crictl pull"',
    )
    parser.add_argument(
        "--ready-timeout-sec",
        type=float,
        default=DEFAULT_READY_TIMEOUT_SEC,
        help="Seconds to wait for each ksvc to become Ready",
    )
    parser.add_argument(
        "--scale-zero-timeout-sec",
        type=float,
        default=DEFAULT_SCALE_ZERO_TIMEOUT_SEC,
        help="Seconds to wait for each ksvc to have zero pods",
    )
    parser.add_argument(
        "--poll-interval-sec",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SEC,
        help="Polling interval for kubectl waits",
    )
    parser.add_argument(
        "--no-wait-ready",
        action="store_true",
        help="Do not wait for each ksvc Ready condition after apply",
    )
    parser.add_argument(
        "--no-wait-scale-zero",
        action="store_true",
        help="Do not wait for each ksvc to scale to zero before applying the next one",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write generated YAML and print commands without running SSH or kubectl",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color in script output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_color(args)
    try:
        run_main(args)
    except KsvcGenerationError as exc:
        print_error(str(exc))
        return 2
    return 0


def run_main(args: argparse.Namespace) -> None:
    if args.count < 1:
        raise KsvcGenerationError("--count must be >= 1")
    if args.poll_interval_sec <= 0:
        raise KsvcGenerationError("--poll-interval-sec must be > 0")

    template_path = Path(args.template)
    output_dir = Path(args.output_dir)
    documents = load_yaml_documents(template_path)
    service_doc = find_knative_service(documents)
    base_name = args.base_name or service_doc["metadata"]["name"]
    namespace = args.namespace or service_doc.get("metadata", {}).get("namespace", "default")
    images = sorted(extract_images(documents))
    min_scale_one = template_min_scale_is_one(service_doc)
    wait_scale_zero = not args.no_wait_scale_zero
    if min_scale_one:
        warn_min_scale_one(service_doc, wait_scale_zero=wait_scale_zero)
        wait_scale_zero = False

    print_kv("template", template_path)
    print_kv("base_name", base_name)
    print_kv("namespace", namespace)
    print_kv("count", args.count)
    print_kv("images", ",".join(images) if images else "<none>")

    if args.pull_images:
        if not args.nodes:
            raise KsvcGenerationError("--nodes is required when --pull-images is used")
        nodes = load_nodes(Path(args.nodes))
        pull_images_on_nodes(
            nodes,
            images,
            ssh_port=args.ssh_port,
            ssh_key=args.ssh_key,
            crictl_command=args.crictl_command,
            dry_run=args.dry_run,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    generated = render_services(
        documents,
        base_name=base_name,
        count=args.count,
        suffix_template=args.suffix_template,
        output_dir=output_dir,
        namespace_override=args.namespace,
    )

    print_kv("generated_dir", output_dir)
    print_kv("generated_files", len(generated))

    if args.apply:
        ensure_namespace(
            namespace,
            kube_context=args.kube_context,
            create=args.create_namespace,
            dry_run=args.dry_run,
        )
        for service_name, path in generated:
            apply_service(
                path,
                service_name=service_name,
                namespace=namespace,
                kube_context=args.kube_context,
                wait_ready=not args.no_wait_ready,
                wait_scale_zero=wait_scale_zero,
                ready_timeout_sec=args.ready_timeout_sec,
                scale_zero_timeout_sec=args.scale_zero_timeout_sec,
                poll_interval_sec=args.poll_interval_sec,
                dry_run=args.dry_run,
            )

    print_success("done")


def load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise KsvcGenerationError(f"template not found: {path}")

    with path.open() as handle:
        loaded = [doc for doc in yaml.safe_load_all(handle) if doc is not None]

    if not loaded:
        raise KsvcGenerationError(f"template contains no YAML documents: {path}")
    if not all(isinstance(doc, dict) for doc in loaded):
        raise KsvcGenerationError("every YAML document must be a mapping")
    return loaded


def find_knative_service(documents: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        doc
        for doc in documents
        if doc.get("apiVersion") == "serving.knative.dev/v1"
        and doc.get("kind") == "Service"
    ]
    if len(matches) != 1:
        raise KsvcGenerationError(
            "template must contain exactly one Knative Service "
            "(apiVersion serving.knative.dev/v1, kind Service)"
        )
    metadata = matches[0].setdefault("metadata", {})
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise KsvcGenerationError("Knative Service template must have metadata.name")
    return matches[0]


def extract_images(value: Any) -> set[str]:
    images: set[str] = set()
    if isinstance(value, dict):
        image = value.get("image")
        if isinstance(image, str) and image:
            images.add(image)
        for child in value.values():
            images.update(extract_images(child))
    elif isinstance(value, list):
        for child in value:
            images.update(extract_images(child))
    return images


def load_nodes(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise KsvcGenerationError(f"nodes file not found: {path}")
    loaded = json.loads(path.read_text())
    if isinstance(loaded, dict) and "nodes" in loaded:
        loaded = loaded["nodes"]
    elif isinstance(loaded, dict):
        loaded = [loaded]
    if not isinstance(loaded, list):
        raise KsvcGenerationError("nodes file must contain a JSON array or a {\"nodes\": [...]} object")

    nodes: list[dict[str, str]] = []
    for index, node in enumerate(loaded, start=1):
        if not isinstance(node, dict):
            raise KsvcGenerationError(f"node {index} must be a JSON object")
        missing = {"hostname", "username", "ip"} - set(node)
        if missing:
            raise KsvcGenerationError(f"node {index} missing field(s): {', '.join(sorted(missing))}")
        loaded_node = {
            "hostname": str(node["hostname"]),
            "username": str(node["username"]),
            "ip": str(node["ip"]),
        }
        sudo_password = str(node.get("sudo_password", ""))
        if sudo_password:
            loaded_node["sudo_password"] = sudo_password
        nodes.append(loaded_node)
    return nodes


def pull_images_on_nodes(
    nodes: list[dict[str, str]],
    images: list[str],
    *,
    ssh_port: int,
    ssh_key: str | None,
    crictl_command: str,
    dry_run: bool,
) -> None:
    if not images:
        print_warning("no images found in template; skipping image pre-pull")
        return

    for node in nodes:
        target = f"{node['username']}@{node['ip']}"
        sudo_password = node.get("sudo_password") or None
        for image in images:
            remote_command, stdin_text = build_remote_pull_command(
                crictl_command,
                image,
                sudo_password=sudo_password,
            )
            command = build_ssh_command(target, remote_command, ssh_port=ssh_port, ssh_key=ssh_key)
            print_step(f"pull_image node={node['hostname']} image={image}")
            run(command, dry_run=dry_run, stdin_text=stdin_text)


def build_remote_pull_command(
    crictl_command: str,
    image: str,
    *,
    sudo_password: str | None,
) -> tuple[str, str | None]:
    command_prefix = crictl_command.strip()
    stdin_text = None
    if sudo_password and command_uses_sudo(command_prefix):
        command_prefix = sudo_stdin_command(command_prefix)
        stdin_text = sudo_password + "\n"
    return f"{command_prefix} {shell_quote(image)}", stdin_text


def command_uses_sudo(command_prefix: str) -> bool:
    return command_prefix == "sudo" or command_prefix.startswith("sudo ")


def sudo_stdin_command(command_prefix: str) -> str:
    if command_prefix.startswith("sudo -n "):
        return "sudo -S -p '' " + command_prefix[len("sudo -n ") :]
    if command_prefix == "sudo":
        return "sudo -S -p ''"
    return "sudo -S -p '' " + command_prefix[len("sudo ") :]


def build_ssh_command(
    target: str,
    remote_command: str,
    *,
    ssh_port: int,
    ssh_key: str | None,
) -> list[str]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        str(ssh_port),
    ]
    if ssh_key:
        command.extend(["-i", ssh_key])
    command.extend([target, remote_command])
    return command


def render_services(
    documents: list[dict[str, Any]],
    *,
    base_name: str,
    count: int,
    suffix_template: str,
    output_dir: Path,
    namespace_override: str | None,
) -> list[tuple[str, Path]]:
    generated: list[tuple[str, Path]] = []
    for index in range(1, count + 1):
        service_name = format_service_name(suffix_template, base_name, index)
        rendered = copy.deepcopy(documents)
        service_doc = find_knative_service(rendered)
        service_doc.setdefault("metadata", {})["name"] = service_name
        if namespace_override is not None:
            service_doc.setdefault("metadata", {})["namespace"] = namespace_override
        path = output_dir / f"{service_name}.yaml"
        with path.open("w") as handle:
            yaml.safe_dump_all(rendered, handle, sort_keys=False)
        generated.append((service_name, path))
    return generated


def format_service_name(suffix_template: str, base_name: str, index: int) -> str:
    try:
        return suffix_template.format(base=base_name, service_base=base_name, index=index)
    except Exception as exc:  # noqa: BLE001 - include the user-provided template.
        raise KsvcGenerationError(f"invalid --suffix-template: {suffix_template}") from exc


def template_min_scale_is_one(service_doc: dict[str, Any]) -> bool:
    annotation = find_min_scale_annotation(service_doc)
    if annotation is None:
        return False
    _, value = annotation
    return str(value).strip() == "1"


def find_min_scale_annotation(service_doc: dict[str, Any]) -> tuple[str, Any] | None:
    annotations = (
        service_doc.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("annotations", {})
    )
    if not isinstance(annotations, dict):
        return None
    for key in (
        "autoscaling.knative.dev/min-scale",
        "autoscaling.knative.dev/minScale",
    ):
        if key in annotations:
            return key, annotations[key]
    return None


def warn_min_scale_one(service_doc: dict[str, Any], *, wait_scale_zero: bool) -> None:
    annotation = find_min_scale_annotation(service_doc)
    if annotation is None:
        return
    key, value = annotation
    print_warning(f"template annotation {key}={value!r} will be preserved")
    if wait_scale_zero:
        print_warning("scale-to-zero wait is skipped because min scale 1 keeps a pod running")


def ensure_namespace(
    namespace: str,
    *,
    kube_context: str | None,
    create: bool,
    dry_run: bool,
) -> None:
    print_step(f"check_namespace name={namespace}")
    if dry_run:
        kubectl(["get", "namespace", namespace], kube_context=kube_context, dry_run=True)
        if create:
            print_step(f"create_namespace name={namespace} if missing")
            kubectl(["create", "namespace", namespace], kube_context=kube_context, dry_run=True)
        return

    if namespace_exists(namespace, kube_context=kube_context):
        return

    if not create:
        raise KsvcGenerationError(
            f"namespace {namespace!r} not found. Create it with "
            f"'kubectl create namespace {namespace}', rerun with --create-namespace, "
            "or use --namespace <existing-namespace>."
        )

    print_step(f"create_namespace name={namespace}")
    kubectl(["create", "namespace", namespace], kube_context=kube_context, dry_run=False)


def namespace_exists(namespace: str, *, kube_context: str | None) -> bool:
    command = build_kubectl_command(["get", "namespace", namespace], kube_context=kube_context)
    printable = " ".join(shell_quote(part) for part in command)
    print_command(printable)
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.returncode == 0


def apply_service(
    path: Path,
    *,
    service_name: str,
    namespace: str,
    kube_context: str | None,
    wait_ready: bool,
    wait_scale_zero: bool,
    ready_timeout_sec: float,
    scale_zero_timeout_sec: float,
    poll_interval_sec: float,
    dry_run: bool,
) -> None:
    print_step(f"apply_ksvc name={service_name} file={path}")
    kubectl(["apply", "-f", str(path)], kube_context=kube_context, dry_run=dry_run)

    if wait_ready:
        print_step(f"wait_ready name={service_name}")
        kubectl(
            [
                "wait",
                "ksvc",
                service_name,
                "-n",
                namespace,
                "--for=condition=Ready",
                f"--timeout={int(ready_timeout_sec)}s",
            ],
            kube_context=kube_context,
            dry_run=dry_run,
        )

    if wait_scale_zero:
        print_step(f"wait_scale_zero name={service_name}")
        wait_for_scale_zero(
            service_name,
            namespace=namespace,
            kube_context=kube_context,
            timeout_sec=scale_zero_timeout_sec,
            poll_interval_sec=poll_interval_sec,
            dry_run=dry_run,
        )


def wait_for_scale_zero(
    service_name: str,
    *,
    namespace: str,
    kube_context: str | None,
    timeout_sec: float,
    poll_interval_sec: float,
    dry_run: bool,
) -> None:
    selector = f"serving.knative.dev/service={service_name}"
    deadline = time.monotonic() + timeout_sec
    jsonpath = r'jsonpath={range .items[*]}{.metadata.name}{"\n"}{end}'

    while True:
        output = kubectl(
            ["get", "pods", "-n", namespace, "-l", selector, "-o", jsonpath],
            kube_context=kube_context,
            capture_output=True,
            dry_run=dry_run,
        )
        pod_names = [line for line in output.splitlines() if line.strip()]
        if not pod_names:
            return
        if time.monotonic() >= deadline:
            raise KsvcGenerationError(
                f"timeout waiting for {service_name} to scale to zero; "
                f"still found pod(s): {', '.join(pod_names)}"
            )
        time.sleep(poll_interval_sec)


def kubectl(
    args: list[str],
    *,
    kube_context: str | None,
    capture_output: bool = False,
    dry_run: bool,
) -> str:
    command = build_kubectl_command(args, kube_context=kube_context)
    return run(command, capture_output=capture_output, dry_run=dry_run)


def build_kubectl_command(args: list[str], *, kube_context: str | None) -> list[str]:
    command = ["kubectl"]
    if kube_context:
        command.extend(["--context", kube_context])
    command.extend(args)
    return command


def run(
    command: list[str],
    *,
    capture_output: bool = False,
    dry_run: bool,
    stdin_text: str | None = None,
) -> str:
    printable = " ".join(shell_quote(part) for part in command)
    print_command(printable)
    if dry_run:
        return ""

    try:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            input=stdin_text,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
        )
    except subprocess.CalledProcessError as exc:
        if command and command[0] == "ssh" and "sudo" in command[-1]:
            if stdin_text:
                raise KsvcGenerationError(
                    "remote image pull failed even though sudo_password was provided; "
                    "check the password and sudo permission for crictl. Failed command: "
                    f"{printable}"
                ) from exc
            raise KsvcGenerationError(
                "remote image pull failed. Add sudo_password to generateKsvc/nodes.json, "
                "configure passwordless sudo for crictl, or run with --crictl-command "
                "'crictl pull' if your user can access crictl without sudo. Failed command: "
                f"{printable}"
            ) from exc
        raise KsvcGenerationError(
            f"command failed with exit code {exc.returncode}: {printable}"
        ) from exc

    return completed.stdout if capture_output and completed.stdout else ""


def shell_quote(value: str) -> str:
    if value and all(ch.isalnum() or ch in "@%_+=:,./-" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
