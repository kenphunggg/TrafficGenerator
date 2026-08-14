"""Runtime configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only used on older Python.
    import tomli as tomllib  # type: ignore[no-redef]

from .models import (
    LoggingConfig,
    ReplayConfig,
    RequestConfig,
    RoutingConfig,
    ServiceConfig,
    ServiceCpuConfig,
    ServiceNimbusConfig,
    SystemConfig,
    SystemKscbConfig,
    SystemKnativeConfig,
    SystemNimbusConfig,
    SystemReposConfig,
    TargetConfig,
    TraceConfig,
    TrafficConfig,
)

DEFAULT_CONFIG_PATH = Path("trafficgen.config.toml")
DOTENV_PATH = Path(".env")

EnvCaster = Callable[[str], Any]


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def parse_optional_int(value: str) -> int | None:
    stripped = value.strip()
    if stripped == "":
        return None
    return int(stripped)


def parse_optional_float(value: str) -> float | None:
    stripped = value.strip()
    if stripped == "":
        return None
    return float(stripped)


def parse_optional_path(value: str) -> Path | None:
    stripped = value.strip()
    if stripped == "":
        return None
    return Path(stripped)


def parse_optional_string(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None


ENV_TO_KEY: dict[str, tuple[str, EnvCaster]] = {
    "TRACE_FILE": ("trace.file", Path),
    "START_MINUTE": ("trace.start_minute", parse_optional_int),
    "END_MINUTE": ("trace.end_minute", parse_optional_int),
    "TRAFFIC_SCALE": ("traffic.scale", float),
    "DRY_RUN": ("traffic.dry_run", parse_bool),
    "RANDOM_SEED": ("traffic.random_seed", parse_optional_int),
    "CLUSTER_NAMESPACE": ("cluster.namespace", str),
    "SERVICE_BASE": ("target.service_base", parse_optional_string),
    "SERVICE_MAP_FILE": ("target.service_map_file", parse_optional_path),
    "LOCUST_HOST": ("target.host", parse_optional_string),
    "REQUEST_URL_TEMPLATE": ("target.url_template", str),
    "REQUEST_URL": ("target.url_template", str),
    "TARGET_NAMESPACE": ("target.namespace", str),
    "REQUEST_PATH": ("target.path", str),
    "INCREASE_SERVICE": ("routing.increase_service", parse_bool),
    "SUFFIX_TEMPLATE": ("routing.suffix_template", str),
    "REQUEST_TIMEOUT_SEC": ("routing.request_timeout_sec", float),
    "DRY_RUN_ASSUMED_SERVICE_TIME_SEC": (
        "routing.dry_run_assumed_service_time_sec",
        parse_optional_float,
    ),
    "MAX_KSVC_ALIASES": ("routing.max_aliases", parse_optional_int),
    "REQUEST_METHOD": ("request.method", str),
    "HEADERS_FILE": ("request.headers_file", parse_optional_path),
    "REQUEST_ID_HEADER": ("request.request_id_header", str),
    "REQUEST_ID_BODY_FIELD": ("request.request_id_body_field", str),
    "INCLUDE_REQUEST_ID_IN_BODY": ("request.include_request_id_in_body", parse_bool),
    "REQUEST_BODY": ("request.body", parse_optional_string),
    "REQUEST_BODY_FILE": ("request.body_file", parse_optional_path),
    "REQUEST_BODY_TEMPLATE_FILE": ("request.body_template_file", parse_optional_path),
    "CONTENT_TYPE": ("request.content_type", str),
    "LOG_DIR": ("logging.dir", Path),
    "LOG_RESPONSE_BODY": ("logging.log_response_body", parse_bool),
    "MAX_BODY_LOG_BYTES": ("logging.max_body_log_bytes", int),
    "KSCB_REPO": ("system.repos.kscb", parse_optional_path),
    "KNATIVE_REPO": ("system.repos.knative", parse_optional_path),
    "NIMBUS_REPO": ("system.repos.nimbus", parse_optional_path),
    "KSCB_NAMESPACE": ("system.kscb.namespace", str),
    "KNATIVE_NAMESPACE": ("system.knative.namespace", str),
    "NIMBUS_DECIDE_URL": ("system.knative.decide_url", str),
    "NIMBUS_HOST_NODE": ("system.nimbus.host_node", parse_optional_string),
    "NIMBUS_HOST_IP": ("system.nimbus.host_ip", parse_optional_string),
    "NIMBUS_SERVICE_NAMESPACE": ("system.nimbus.service_namespace", str),
}


def _deep_update(base: MutableMapping[str, Any], updates: Mapping[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), MutableMapping):
            _deep_update(base[key], value)  # type: ignore[index]
        else:
            base[key] = value


def _set_dotted(data: MutableMapping[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    cursor: MutableMapping[str, Any] = data
    for part in parts[:-1]:
        child = cursor.setdefault(part, {})
        if not isinstance(child, MutableMapping):
            raise ValueError(f"cannot override {dotted_key}; {part} is not a table")
        cursor = child
    cursor[parts[-1]] = value


def load_dotenv(path: Path = DOTENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_no, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"invalid .env line {line_no}: missing '='")
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_env_value(value.strip())
        if not key:
            raise ValueError(f"invalid .env line {line_no}: empty key")
        values[key] = value
    return values


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        return value.split(" #", 1)[0].rstrip()
    return value


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"config file {path} did not contain a TOML table")
    return loaded


def _coerce_path(value: Any, base_dir: Path) -> Path | None:
    if value is None:
        return None
    path = value if isinstance(value, Path) else Path(str(value))
    if path.is_absolute():
        return path
    return base_dir / path


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return _table(raw.get(name, {}), f"[{name}]")


def _table(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"config section {label} must be a table")
    return value


def load_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    dotenv_path: str | Path | None = DOTENV_PATH,
    require_file: bool = True,
) -> ReplayConfig:
    """Load config using built-in defaults, TOML, .env, then process env.

    ``TRAFFICGEN_CONFIG`` is read before loading the TOML file so containers can
    point at a different config without a CLI flag.
    """

    process_env = dict(os.environ if env is None else env)
    dotenv_values = load_dotenv(Path(dotenv_path)) if dotenv_path is not None else {}

    selected_path = path or process_env.get("TRAFFICGEN_CONFIG") or dotenv_values.get("TRAFFICGEN_CONFIG")
    config_path = Path(selected_path) if selected_path else DEFAULT_CONFIG_PATH

    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = _load_toml(config_path)
    elif require_file and (path is not None or selected_path is not None):
        raise FileNotFoundError(f"config file not found: {config_path}")
    elif require_file and config_path == DEFAULT_CONFIG_PATH:
        raise FileNotFoundError(
            f"config file not found: {config_path}; copy trafficgen.config.example.toml first"
        )

    merged: dict[str, Any] = {}
    _deep_update(merged, raw)

    overrides: dict[str, str] = {}
    overrides.update(dotenv_values)
    overrides.update(process_env)
    for env_name, (dotted_key, caster) in ENV_TO_KEY.items():
        if env_name not in overrides:
            continue
        try:
            _set_dotted(merged, dotted_key, caster(overrides[env_name]))
        except Exception as exc:  # noqa: BLE001 - include env name in the error.
            raise ValueError(f"invalid value for {env_name}: {exc}") from exc

    config = _build_config(merged, config_path)
    return validate_config(config)


def _build_config(raw: Mapping[str, Any], config_path: Path) -> ReplayConfig:
    base_dir = config_path.parent if config_path.parent != Path("") else Path(".")

    trace = _section(raw, "trace")
    traffic = _section(raw, "traffic")
    cluster = _section(raw, "cluster")
    target = _section(raw, "target")
    routing = _section(raw, "routing")
    request = _section(raw, "request")
    logging = _section(raw, "logging")
    services = _build_services(raw.get("services", []), base_dir)
    first_service = services[0] if services else None

    namespace = str(target.get("namespace", cluster.get("namespace", "serverless")))
    default_path = first_service.path if first_service and first_service.path else "/detect/local"
    default_url_template = (
        first_service.url_template
        if first_service and first_service.url_template
        else "http://{service}.{namespace}.svc.cluster.local{path}"
    )

    return ReplayConfig(
        trace=TraceConfig(
            file=_coerce_path(trace.get("file"), base_dir),
            start_minute=_optional_int(trace.get("start_minute")),
            end_minute=_optional_int(trace.get("end_minute")),
        ),
        traffic=TrafficConfig(
            scale=float(traffic.get("scale", 1.0)),
            dry_run=parse_bool(traffic.get("dry_run", False)),
            random_seed=_optional_int(traffic.get("random_seed")),
        ),
        target=TargetConfig(
            service_base=_coerce_optional_str(target.get("service_base"))
            or (first_service.service_base if first_service is not None else None),
            service_map_file=_coerce_path(target.get("service_map_file"), base_dir),
            host=_coerce_optional_str(target.get("host")),
            url_template=str(target.get("url_template", default_url_template)),
            namespace=namespace,
            path=str(target.get("path", default_path)),
        ),
        services=services,
        system=_build_system_config(raw, base_dir),
        routing=RoutingConfig(
            increase_service=parse_bool(routing.get("increase_service", False)),
            suffix_template=str(routing.get("suffix_template", "{service_base}-{index:03d}")),
            request_timeout_sec=float(routing.get("request_timeout_sec", 300.0)),
            dry_run_assumed_service_time_sec=_optional_float(
                routing.get("dry_run_assumed_service_time_sec")
            ),
            max_aliases=_optional_int(routing.get("max_aliases")),
        ),
        request=RequestConfig(
            method=str(request.get("method", "POST")).upper(),
            headers_file=_coerce_path(request.get("headers_file"), base_dir),
            request_id_header=str(
                request.get("request_id_header", "X-Trafficgen-Request-Id")
            ),
            request_id_body_field=str(request.get("request_id_body_field", "request_id")),
            include_request_id_in_body=parse_bool(
                request.get("include_request_id_in_body", True)
            ),
            body=_coerce_optional_str(request.get("body")),
            body_file=_coerce_path(request.get("body_file"), base_dir),
            body_template_file=_coerce_path(request.get("body_template_file"), base_dir),
            content_type=str(request.get("content_type", "application/json")),
        ),
        logging=LoggingConfig(
            dir=_coerce_path(logging.get("dir", "logs"), base_dir) or Path("logs"),
            log_response_body=parse_bool(logging.get("log_response_body", True)),
            max_body_log_bytes=int(logging.get("max_body_log_bytes", 65536)),
        ),
        config_path=config_path,
    )


def _build_system_config(raw: Mapping[str, Any], base_dir: Path) -> SystemConfig:
    system = _section(raw, "system")
    repos = _table(system.get("repos", {}), "[system.repos]")
    kscb = _table(system.get("kscb", {}), "[system.kscb]")
    knative = _table(system.get("knative", {}), "[system.knative]")
    nimbus = _table(system.get("nimbus", {}), "[system.nimbus]")

    return SystemConfig(
        repos=SystemReposConfig(
            kscb=_coerce_path(repos.get("kscb"), base_dir),
            knative=_coerce_path(repos.get("knative"), base_dir),
            nimbus=_coerce_path(repos.get("nimbus"), base_dir),
        ),
        kscb=SystemKscbConfig(
            namespace=str(kscb.get("namespace", "kube-startup-cpu-boost-system")),
        ),
        knative=SystemKnativeConfig(
            namespace=str(knative.get("namespace", "knative-serving")),
            decide_url=str(
                knative.get(
                    "decide_url",
                    "http://nimbus.knative-serving.svc.cluster.local:8080/decide",
                )
            ),
        ),
        nimbus=SystemNimbusConfig(
            host_node=_coerce_optional_str(nimbus.get("host_node")),
            host_ip=_coerce_optional_str(nimbus.get("host_ip")),
            service_namespace=str(nimbus.get("service_namespace", "knative-serving")),
        ),
    )


def _build_services(raw_value: Any, base_dir: Path) -> tuple[ServiceConfig, ...]:
    if raw_value in (None, ""):
        return ()
    if not isinstance(raw_value, list):
        raise ValueError("config section [[services]] must be an array of tables")

    services: list[ServiceConfig] = []
    for index, raw_service in enumerate(raw_value, start=1):
        if not isinstance(raw_service, Mapping):
            raise ValueError(f"config section [[services]] item {index} must be a table")
        service_base = _coerce_optional_str(raw_service.get("service_base"))
        if service_base is None:
            raise ValueError(f"services[{index}].service_base is required")
        cpu = _table(raw_service.get("cpu", {}), f"services[{index}].cpu")
        nimbus = _table(raw_service.get("nimbus", {}), f"services[{index}].nimbus")
        services.append(
            ServiceConfig(
                service_base=service_base,
                trace_apps=_tuple_str(raw_service.get("trace_apps")),
                path=_coerce_optional_str(raw_service.get("path")),
                url_template=_coerce_optional_str(raw_service.get("url_template")),
                ksvc_template=_coerce_path(raw_service.get("ksvc_template"), base_dir),
                alias_count=_optional_alias_count(raw_service.get("alias_count")),
                cpu=ServiceCpuConfig(
                    cpu_budget_static=_coerce_optional_str(cpu.get("cpu_budget_static")),
                    c_opt_cold=_coerce_optional_str(cpu.get("c_opt_cold")),
                    c_opt_warm=_coerce_optional_str(cpu.get("c_opt_warm")),
                    c_min_cold=_coerce_optional_str(cpu.get("c_min_cold")),
                    c_min_warm=_coerce_optional_str(cpu.get("c_min_warm")),
                ),
                nimbus=ServiceNimbusConfig(
                    node_pool_selector=_coerce_optional_str(nimbus.get("node_pool_selector")),
                    pre_measured_dir=_coerce_path(nimbus.get("pre_measured_dir"), base_dir),
                ),
            )
        )
    return tuple(services)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_alias_count(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() == "auto":
        return None
    count = int(value)
    if count < 1:
        raise ValueError("services.alias_count must be >= 1 or 'auto'")
    return count


def _tuple_str(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        raw_items: Sequence[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        raise ValueError("services.trace_apps must be a string or array of strings")
    items = tuple(str(item).strip() for item in raw_items if str(item).strip())
    return items


def validate_config(config: ReplayConfig) -> ReplayConfig:
    warnings: list[str] = []

    if config.trace.file is None:
        raise ValueError("trace.file is required")
    if config.trace.start_minute is not None and config.trace.start_minute < 0:
        raise ValueError("trace.start_minute must be >= 0")
    if config.trace.end_minute is not None and config.trace.end_minute < 0:
        raise ValueError("trace.end_minute must be >= 0")
    if (
        config.trace.start_minute is not None
        and config.trace.end_minute is not None
        and config.trace.start_minute > config.trace.end_minute
    ):
        raise ValueError("trace.start_minute must be <= trace.end_minute")

    if config.traffic.scale < 0:
        raise ValueError("traffic.scale must be >= 0")
    if config.traffic.scale > 1:
        warnings.append("traffic.scale is greater than 1.0; replay will exceed original traffic")

    if config.request.method != "POST":
        raise ValueError("request.method must be POST in the first implementation")
    if config.routing.request_timeout_sec <= 0:
        raise ValueError("routing.request_timeout_sec must be > 0")
    if (
        config.routing.dry_run_assumed_service_time_sec is not None
        and config.routing.dry_run_assumed_service_time_sec < 0
    ):
        raise ValueError("routing.dry_run_assumed_service_time_sec must be >= 0")
    if config.routing.max_aliases is not None and config.routing.max_aliases < 1:
        raise ValueError("routing.max_aliases must be >= 1")
    if not config.target.path.startswith("/"):
        raise ValueError("target.path must start with '/'")
    if config.logging.max_body_log_bytes < 0:
        raise ValueError("logging.max_body_log_bytes must be >= 0")

    seen_services: set[str] = set()
    for service in config.services:
        if service.service_base in seen_services:
            raise ValueError(f"duplicate services.service_base: {service.service_base}")
        seen_services.add(service.service_base)
        if service.path is not None and not service.path.startswith("/"):
            raise ValueError(f"services[{service.service_base}].path must start with '/'")

    body_sources = [config.request.body, config.request.body_file, config.request.body_template_file]
    configured_body_sources = [value for value in body_sources if value is not None]
    if len(configured_body_sources) > 1:
        raise ValueError(
            "configure at most one request body source: request.body, "
            "request.body_file, or request.body_template_file"
        )

    if not config.target.service_base and not config.target.service_map_file:
        warnings.append(
            "target.service_base and target.service_map_file are unset; "
            "trace function_id values will be used as service bases"
        )

    return replace(config, warnings=tuple(warnings))
