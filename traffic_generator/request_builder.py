"""Build HTTP requests from replay events and routing decisions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .models import AliasAllocation, BuiltRequest, ReplayConfig, RequestEvent

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class RequestBuildError(ValueError):
    """Raised when request configuration cannot produce a valid request."""


class RequestBuilder:
    def __init__(self, config: ReplayConfig) -> None:
        self.config = config
        self._headers = _load_headers(config.request.headers_file)
        self._body_source = _BodySource(config)

    def build(self, event: RequestEvent, allocation: AliasAllocation) -> BuiltRequest:
        service = _service_config(self.config, event.service_base)
        path = service.path if service is not None and service.path else self.config.target.path
        url_template = (
            service.url_template
            if service is not None and service.url_template
            else self.config.target.url_template
        )
        context = _context(event, allocation, self.config, path=path)
        url = resolve_url(
            render_template(url_template, context),
            self.config.target.host,
        )
        headers = dict(self._headers)
        headers[self.config.request.request_id_header] = str(event.request_id)

        body, body_for_log = self._body_source.render(context, event.request_id)
        if body is not None and not _has_header(headers, "content-type"):
            headers["Content-Type"] = self.config.request.content_type

        return BuiltRequest(
            event=event,
            allocation=allocation,
            method=self.config.request.method,
            url=url,
            headers=headers,
            body=body,
            body_for_log=body_for_log,
        )


class _BodySource:
    def __init__(self, config: ReplayConfig) -> None:
        self.config = config
        self.inline_body = config.request.body
        self.body_text = _read_optional_text(config.request.body_file)
        self.template_text = _read_optional_text(config.request.body_template_file)

    def render(self, context: Mapping[str, Any], request_id: int) -> tuple[bytes | None, Any]:
        if self.template_text is not None:
            rendered = render_template(self.template_text, context)
            return self._encode_body(rendered, request_id)
        if self.body_text is not None:
            return self._encode_body(self.body_text, request_id)
        if self.inline_body is not None:
            return self._encode_body(self.inline_body, request_id)
        if self.config.request.include_request_id_in_body:
            payload = {self.config.request.request_id_body_field: request_id}
            return _json_bytes(payload), payload
        return None, None

    def _encode_body(self, text: str, request_id: int) -> tuple[bytes, Any]:
        if _is_json_content_type(self.config.request.content_type):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RequestBuildError("configured JSON request body is invalid") from exc
            if (
                self.config.request.include_request_id_in_body
                and isinstance(parsed, dict)
                and self.config.request.request_id_body_field not in parsed
            ):
                parsed[self.config.request.request_id_body_field] = request_id
            return _json_bytes(parsed), parsed
        return text.encode("utf-8"), text


def resolve_url(url: str, host: str | None) -> str:
    if url.startswith(("http://", "https://")) or not host:
        return url
    return f"{host.rstrip('/')}/{url.lstrip('/')}"


def render_template(template: str, context: Mapping[str, Any]) -> str:
    required_fields = set(_PLACEHOLDER_RE.findall(template))
    missing = required_fields - set(context)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise RequestBuildError(f"template references unknown field(s): {missing_list}")

    def replace_placeholder(match: re.Match[str]) -> str:
        return str(context[match.group(1)])

    return _PLACEHOLDER_RE.sub(replace_placeholder, template)


def _context(
    event: RequestEvent,
    allocation: AliasAllocation,
    config: ReplayConfig,
    *,
    path: str,
) -> dict[str, Any]:
    return {
        "service": allocation.target_service,
        "service_base": event.service_base,
        "namespace": config.target.namespace,
        "path": path,
        "function_id": event.function_id,
        "minute": event.trace_minute,
        "arrival_offset_sec": event.arrival_offset_sec,
        "scheduled_at_sec": event.scheduled_at_sec,
        "request_index": event.request_index,
        "request_id": event.request_id,
        "alias_index": allocation.alias_index,
        "alias_decision": allocation.decision,
    }


def _service_config(config: ReplayConfig, service_base: str):
    for service in config.services:
        if service.service_base == service_base:
            return service
    return None


def _load_headers(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"headers file not found: {path}")
    loaded = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise RequestBuildError("headers file must contain a JSON object")
    headers: dict[str, str] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise RequestBuildError("header names must be strings")
        headers[key] = str(value)
    return headers


def _read_optional_text(path: Path | None) -> str | None:
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"body file not found: {path}")
    return path.read_text()


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    return any(existing.lower() == name.lower() for existing in headers)


def _is_json_content_type(content_type: str) -> bool:
    return "json" in content_type.lower()


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def built_request_metadata(request: BuiltRequest) -> dict[str, Any]:
    event = request.event
    allocation = request.allocation
    return {
        "request_id": event.request_id,
        "trace_minute": event.trace_minute,
        "arrival_offset_sec": event.arrival_offset_sec,
        "scheduled_at_sec": event.scheduled_at_sec,
        "function_id": event.function_id,
        "service_base": event.service_base,
        "target_service": allocation.target_service,
        "alias_index": allocation.alias_index,
        "alias_decision": allocation.decision,
        "method": request.method,
        "url": request.url,
        "headers": dict(request.headers),
        "request_body_bytes": request.body_size,
        "request_body": request.body_for_log,
    }
