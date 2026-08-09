"""Core dataclasses used by the traffic generator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class TraceRow:
    minute: int
    function_id: str
    count: int


@dataclass(frozen=True)
class TraceConfig:
    file: Path | None = None
    start_minute: int | None = None
    end_minute: int | None = None


@dataclass(frozen=True)
class TrafficConfig:
    scale: float = 1.0
    dry_run: bool = False
    random_seed: int | None = None


@dataclass(frozen=True)
class TargetConfig:
    service_base: str | None = None
    service_map_file: Path | None = None
    host: str | None = None
    url_template: str = "http://{service}.{namespace}.svc.cluster.local{path}"
    namespace: str = "serverless"
    path: str = "/detect/local"


@dataclass(frozen=True)
class RoutingConfig:
    increase_service: bool = False
    suffix_template: str = "{service_base}-{index:03d}"
    request_timeout_sec: float = 300.0
    dry_run_assumed_service_time_sec: float | None = None
    max_aliases: int | None = None


@dataclass(frozen=True)
class RequestConfig:
    method: str = "POST"
    headers_file: Path | None = None
    request_id_header: str = "X-Trafficgen-Request-Id"
    request_id_body_field: str = "request_id"
    include_request_id_in_body: bool = True
    body: str | None = None
    body_file: Path | None = None
    body_template_file: Path | None = None
    content_type: str = "application/json"


@dataclass(frozen=True)
class LoggingConfig:
    dir: Path = Path("logs")
    log_response_body: bool = True
    max_body_log_bytes: int = 65536


@dataclass(frozen=True)
class ReplayConfig:
    trace: TraceConfig = field(default_factory=TraceConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    request: RequestConfig = field(default_factory=RequestConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    config_path: Path | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestEvent:
    request_id: int
    trace_minute: int
    arrival_offset_sec: float
    scheduled_at_sec: float
    function_id: str
    service_base: str
    request_index: int


@dataclass(frozen=True)
class AliasAllocation:
    service_base: str
    target_service: str
    alias_index: int | None
    decision: str


@dataclass(frozen=True)
class BuiltRequest:
    event: RequestEvent
    allocation: AliasAllocation
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    body_for_log: Any

    @property
    def body_size(self) -> int:
        return 0 if self.body is None else len(self.body)


@dataclass
class AliasState:
    next_suffix: int = 1
    free_aliases: list[int] = field(default_factory=list)
    busy_aliases: set[int] = field(default_factory=set)
    quarantined_aliases: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class DryRunSummary:
    trace_file: Path
    scale: float
    minutes: int
    original_requests: int
    scheduled_requests: int
    method: str
    namespace: str
    path: str
    increase_service: bool
    max_allocated_alias_by_service: Mapping[str, int]
    log_dir: Path
