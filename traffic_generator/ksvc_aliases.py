"""Config-backed Knative Service alias preparation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only used on older Python.
    import tomli as tomllib  # type: ignore[no-redef]

from .config import parse_bool


@dataclass(frozen=True)
class KsvcAliasConfig:
    enabled: bool = False
    template: Path | None = None
    count: int = 0
    output_dir: Path = Path("generated-ksvc")
    base_name: str | None = None
    suffix_template: str = "{base}-{index:03d}"
    namespace: str | None = None
    nodes: Path | None = None
    pull_images: bool = False
    create_namespace: bool = False
    apply: bool = False
    no_wait_ready: bool = False
    no_wait_scale_zero: bool = False


def load_ksvc_alias_config(path: str | Path) -> KsvcAliasConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"traffic config not found: {config_path}")
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"traffic config {config_path} did not contain a TOML table")

    base_dir = config_path.parent if config_path.parent != Path("") else Path(".")
    aliases = _section(raw, "ksvc_aliases")
    target = _section(raw, "target")
    routing = _section(raw, "routing")
    count = int(aliases.get("count", 0))
    enabled = parse_bool(aliases.get("enabled", False))

    config = KsvcAliasConfig(
        enabled=enabled,
        template=_optional_path(aliases.get("template"), base_dir),
        count=count,
        output_dir=_coerce_path(aliases.get("output_dir", "generated-ksvc"), base_dir),
        base_name=_optional_str(aliases.get("base_name", target.get("service_base"))),
        suffix_template=str(
            aliases.get(
                "suffix_template",
                _trafficgen_suffix_to_generator_suffix(
                    str(routing.get("suffix_template", "{service_base}-{index:03d}"))
                ),
            )
        ),
        namespace=_optional_str(aliases.get("namespace", target.get("namespace"))),
        nodes=_optional_path(aliases.get("nodes"), base_dir),
        pull_images=parse_bool(aliases.get("pull_images", False)),
        create_namespace=parse_bool(aliases.get("create_namespace", False)),
        apply=parse_bool(aliases.get("apply", False)),
        no_wait_ready=parse_bool(aliases.get("no_wait_ready", False)),
        no_wait_scale_zero=parse_bool(aliases.get("no_wait_scale_zero", False)),
    )
    return validate_ksvc_alias_config(config)


def validate_ksvc_alias_config(config: KsvcAliasConfig) -> KsvcAliasConfig:
    if not config.enabled:
        return config
    if config.template is None:
        raise ValueError("ksvc_aliases.template is required when aliases are enabled")
    if config.pull_images and config.nodes is None:
        raise ValueError("ksvc_aliases.nodes is required when pull_images = true")
    return config


def with_ksvc_alias_count(config: KsvcAliasConfig, count: int) -> KsvcAliasConfig:
    if count < 1:
        raise ValueError("computed KService alias count must be >= 1")
    return replace(config, count=count)


def expected_ksvc_alias_names(config: KsvcAliasConfig) -> list[str]:
    if not config.enabled:
        return []
    if config.count < 1:
        raise ValueError("KService alias count has not been computed")
    if not config.base_name:
        raise ValueError(
            "ksvc_aliases.base_name or target.service_base is required to preflight aliases"
        )
    return [
        _format_alias_name(config.suffix_template, config.base_name, index)
        for index in range(1, config.count + 1)
    ]


def build_check_ksvc_aliases_command(config: KsvcAliasConfig) -> list[str]:
    names = expected_ksvc_alias_names(config)
    if not names:
        return []
    namespace = config.namespace or "default"
    return ["kubectl", "get", "ksvc", "-n", namespace, *names]


def all_ksvc_aliases_exist(config: KsvcAliasConfig) -> bool:
    if not config.enabled or not config.apply:
        return False
    try:
        command = build_check_ksvc_aliases_command(config)
    except ValueError:
        return False
    if not command:
        return False
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return False
    return completed.returncode == 0


def _format_alias_name(suffix_template: str, base_name: str, index: int) -> str:
    try:
        return suffix_template.format(base=base_name, service_base=base_name, index=index)
    except Exception as exc:  # noqa: BLE001 - include the user-provided template.
        raise ValueError(f"invalid ksvc_aliases.suffix_template: {suffix_template}") from exc


def build_generate_ksvc_command(config: KsvcAliasConfig) -> list[str]:
    if not config.enabled:
        return []
    if config.template is None:
        raise ValueError("ksvc_aliases.template is required when aliases are enabled")
    if config.count < 1:
        raise ValueError("KService alias count has not been computed")

    script = Path(__file__).resolve().parents[1] / "generateKsvc" / "generate_ksvc.py"
    command = [
        sys.executable,
        str(script),
        "--template",
        str(config.template),
        "--count",
        str(config.count),
        "--output-dir",
        str(config.output_dir),
        "--suffix-template",
        config.suffix_template,
    ]
    if config.base_name:
        command.extend(["--base-name", config.base_name])
    if config.namespace:
        command.extend(["--namespace", config.namespace])
    if config.nodes:
        command.extend(["--nodes", str(config.nodes)])
    if config.pull_images:
        command.append("--pull-images")
    if config.create_namespace:
        command.append("--create-namespace")
    if config.apply:
        command.append("--apply")
    if config.no_wait_ready:
        command.append("--no-wait-ready")
    if config.no_wait_scale_zero:
        command.append("--no-wait-scale-zero")
    return command


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"config section [{name}] must be a table")
    return value


def _coerce_path(value: Any, base_dir: Path) -> Path:
    path = value if isinstance(value, Path) else Path(str(value))
    if path.is_absolute():
        return path
    return base_dir / path


def _optional_path(value: Any, base_dir: Path) -> Path | None:
    if value is None or value == "":
        return None
    return _coerce_path(value, base_dir)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _trafficgen_suffix_to_generator_suffix(template: str) -> str:
    return template.replace("{service_base}", "{base}")
