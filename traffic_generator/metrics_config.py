"""Configuration for offline metrics collection and plotting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only used on older Python.
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CENTRAL_CONFIG_PATH = Path("trafficgen.config.toml")

DEFAULT_CONFIGURATION_LABELS = {
    "static-knative": "Static Knative",
    "fixed-boost": "Fixed Startup Boost",
    "full-nimbus": "Full Nimbus",
}

DEFAULT_PROMQL = {
    "ready_pods": (
        'sum(kube_pod_status_ready{namespace="$namespace",condition="true",'
        'pod=~"$service_regex"})'
    ),
    "pending_pods": (
        'sum(kube_pod_status_phase{namespace="$namespace",phase="Pending",'
        'pod=~"$service_regex"})'
    ),
    "allocated_cpu_cores": (
        'sum(kube_pod_container_resource_requests{namespace="$namespace",'
        'resource="cpu",pod=~"$service_regex"})'
    ),
    "actual_cpu_cores": (
        'sum(rate(container_cpu_usage_seconds_total{namespace="$namespace",'
        'pod=~"$service_regex",container!="",container!="POD"}[1m]))'
    ),
    "nimbus_mode": "",
    "nimbus_tier": "",
}


@dataclass(frozen=True)
class MetricsConfig:
    results_dir: Path = Path("results")
    output_dir: Path = Path("results/metrics")
    plots_dir: Path = Path("results/plots")
    prometheus_url: str | None = None
    namespace: str = "serverless"
    service_base: str = "measure-yolo"
    service_regex: str = "measure-yolo.*"
    step_sec: int = 60
    warm_slo_ms: float = 1500.0
    cold_slo_ms: float = 15000.0
    request_timeout_sec: float = 300.0
    configuration_labels: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CONFIGURATION_LABELS)
    )
    promql: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROMQL))


def load_metrics_config(path: str | Path | None = None) -> MetricsConfig:
    """Load metrics settings from `[metrics]` in `trafficgen.config.toml`."""

    config_path = Path(path) if path is not None else DEFAULT_CENTRAL_CONFIG_PATH
    raw = _load_toml(config_path) if config_path.exists() else {}
    if path is not None and not config_path.exists():
        raise FileNotFoundError(f"traffic config not found: {config_path}")
    base_dir = config_path.parent if config_path.parent != Path("") else Path(".")
    metrics_raw = _section(raw, "metrics")
    return validate_metrics_config(_build_metrics_config(metrics_raw, base_dir, raw))


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"traffic config {path} did not contain a TOML table")
    return loaded


def _build_metrics_config(
    raw: Mapping[str, Any],
    base_dir: Path,
    root_raw: Mapping[str, Any],
) -> MetricsConfig:
    target = _section(root_raw, "target")
    prometheus = _section(raw, "prometheus")

    service_base_default = str(target.get("service_base", "measure-yolo"))
    namespace_default = str(target.get("namespace", "serverless"))
    service_regex_default = f"{service_base_default}.*"

    labels = dict(DEFAULT_CONFIGURATION_LABELS)
    labels.update({str(key): str(value) for key, value in _section(raw, "configuration_labels").items()})

    queries = dict(DEFAULT_PROMQL)
    queries.update({str(key): str(value) for key, value in _section(prometheus, "queries").items()})

    return MetricsConfig(
        results_dir=_coerce_path(raw.get("results_dir", "results"), base_dir),
        output_dir=_coerce_path(raw.get("output_dir", "results/metrics"), base_dir),
        plots_dir=_coerce_path(raw.get("plots_dir", "results/plots"), base_dir),
        prometheus_url=_optional_str(prometheus.get("url")),
        namespace=str(prometheus.get("namespace", namespace_default)),
        service_base=str(prometheus.get("service_base", service_base_default)),
        service_regex=str(prometheus.get("service_regex", service_regex_default)),
        step_sec=int(raw.get("step_sec", 60)),
        warm_slo_ms=float(raw.get("warm_slo_ms", 1500.0)),
        cold_slo_ms=float(raw.get("cold_slo_ms", 15000.0)),
        request_timeout_sec=float(raw.get("request_timeout_sec", 300.0)),
        configuration_labels=labels,
        promql=queries,
    )


def validate_metrics_config(config: MetricsConfig) -> MetricsConfig:
    if config.step_sec <= 0:
        raise ValueError("metrics.step_sec must be > 0")
    if config.warm_slo_ms <= 0:
        raise ValueError("metrics.warm_slo_ms must be > 0")
    if config.cold_slo_ms <= 0:
        raise ValueError("metrics.cold_slo_ms must be > 0")
    if config.request_timeout_sec <= 0:
        raise ValueError("metrics.request_timeout_sec must be > 0")
    if not config.namespace.strip():
        raise ValueError("metrics.prometheus.namespace must not be empty")
    if not config.service_regex.strip():
        raise ValueError("metrics.prometheus.service_regex must not be empty")
    return config


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"metrics config section [{name}] must be a table")
    return value


def _coerce_path(value: Any, base_dir: Path) -> Path:
    path = value if isinstance(value, Path) else Path(str(value))
    if path.is_absolute():
        return path
    return base_dir / path


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None
