"""Trace function to target service mapping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .models import ReplayConfig


class ServiceMapError(ValueError):
    """Raised when a service map cannot be loaded or validated."""


class ServiceResolver:
    def __init__(self, service_base: str | None, service_map: Mapping[str, str] | None = None):
        self.service_base = service_base
        self.service_map = dict(service_map or {})

    @classmethod
    def from_config(cls, config: ReplayConfig) -> "ServiceResolver":
        service_map: dict[str, str] = {}
        for service in config.services:
            for trace_app in service.trace_apps:
                service_map[trace_app] = service.service_base
        if config.target.service_map_file is not None:
            service_map.update(load_service_map(config.target.service_map_file))
        return cls(config.target.service_base, service_map)

    def resolve(self, function_id: str) -> str:
        if function_id in self.service_map:
            return self.service_map[function_id]
        if self.service_base:
            return self.service_base
        return function_id


def load_service_map(path: str | Path) -> dict[str, str]:
    map_path = Path(path)
    if not map_path.exists():
        raise FileNotFoundError(f"service map not found: {map_path}")

    suffix = map_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data = _load_yaml(map_path)
    else:
        data = json.loads(map_path.read_text())

    if not isinstance(data, dict):
        raise ServiceMapError("service map must be a JSON/YAML object")

    result: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ServiceMapError("service map keys and values must be strings")
        if not key.strip() or not value.strip():
            raise ServiceMapError("service map keys and values must be non-empty")
        result[key] = value
    return result


def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional package.
        raise ServiceMapError(
            "YAML service maps require PyYAML; use JSON or install pyyaml"
        ) from exc
    return yaml.safe_load(path.read_text())
