"""JSONL request and response logging."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import BuiltRequest
from .request_builder import built_request_metadata


class JsonlTrafficLogger:
    def __init__(
        self,
        log_dir: str | Path,
        *,
        log_response_body: bool = True,
        max_body_log_bytes: int = 65536,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_response_body = log_response_body
        self.max_body_log_bytes = max_body_log_bytes
        self._lock = threading.RLock()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.requests_path = self.log_dir / "requests.jsonl"
        self.responses_path = self.log_dir / "responses.jsonl"

    def log_request(self, request: BuiltRequest) -> None:
        record = {
            "timestamp": utc_now_iso(),
            **built_request_metadata(request),
        }
        self._append_json(self.requests_path, record)

    def log_response(
        self,
        request: BuiltRequest,
        *,
        success: bool,
        status_code: int | None,
        response_time_ms: float,
        response_body: bytes | str | None = None,
        error: str | None = None,
    ) -> None:
        parsed_fields, logged_body, truncated = parse_response_body(
            response_body,
            log_response_body=self.log_response_body,
            max_body_log_bytes=self.max_body_log_bytes,
        )
        record: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "request_id": request.event.request_id,
            "trace_minute": request.event.trace_minute,
            "arrival_offset_sec": request.event.arrival_offset_sec,
            "function_id": request.event.function_id,
            "target_service": request.allocation.target_service,
            "alias_index": request.allocation.alias_index,
            "method": request.method,
            "url": request.url,
            "success": success,
            "status_code": status_code,
            "response_time_ms": response_time_ms,
        }
        record.update(parsed_fields)
        if self.log_response_body:
            record["response_body"] = logged_body
            record["response_body_truncated"] = truncated
        if error is not None:
            record["error"] = error
        response_request_id = record.get("response_request_id")
        if response_request_id is not None and response_request_id != request.event.request_id:
            record["request_id_mismatch"] = True
        self._append_json(self.responses_path, record)

    def _append_json(self, path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
        with self._lock:
            with path.open("a") as handle:
                handle.write(line + "\n")


def parse_response_body(
    response_body: bytes | str | None,
    *,
    log_response_body: bool,
    max_body_log_bytes: int,
) -> tuple[dict[str, Any], str | None, bool]:
    if response_body is None:
        return {"response_request_id": None}, None, False

    if isinstance(response_body, str):
        raw = response_body.encode("utf-8", errors="replace")
    else:
        raw = response_body

    parsed_fields: dict[str, Any] = {"response_request_id": None}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        parsed_fields["response_request_id"] = parsed.get("request_id")
        for source, target in [
            ("processing_time_ms", "processing_time_ms"),
            ("cold_start", "cold_start"),
            ("cold_start_time_ms", "cold_start_time_ms"),
            ("pod_name", "pod_name"),
            ("service", "response_service"),
        ]:
            if source in parsed:
                parsed_fields[target] = parsed[source]

    if not log_response_body:
        return parsed_fields, None, False

    truncated = len(raw) > max_body_log_bytes
    clipped = raw[:max_body_log_bytes]
    logged_body = clipped.decode("utf-8", errors="replace")
    return parsed_fields, logged_body, truncated


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
