"""Shared helpers for offline metrics run directories."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .metrics_config import MetricsConfig


@dataclass(frozen=True)
class RunPaths:
    configuration_slug: str
    configuration: str
    run_id: str
    path: Path
    requests_path: Path
    responses_path: Path
    prometheus_path: Path


def discover_run_paths(
    config: MetricsConfig,
    *,
    run_dir: str | Path | None = None,
) -> list[RunPaths]:
    if run_dir is not None:
        return [_run_paths_from_dir(config, Path(run_dir))]

    if not config.results_dir.exists():
        return []

    runs: list[RunPaths] = []
    for configuration_dir in sorted(config.results_dir.iterdir()):
        if not configuration_dir.is_dir():
            continue
        for candidate in sorted(configuration_dir.iterdir()):
            if candidate.is_dir() and _looks_like_run_dir(candidate):
                runs.append(_run_paths_from_dir(config, candidate))
    return runs


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open() as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_no}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"JSONL row in {path}:{line_no} must be an object")
            rows.append(loaded)
    return rows


def parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc


def run_time_window(run: RunPaths) -> tuple[float, float]:
    timestamps = [
        parsed
        for parsed in _iter_log_timestamps(
            [run.requests_path, run.responses_path]
        )
        if parsed is not None
    ]
    if not timestamps:
        raise ValueError(f"cannot determine time window for {run.path}; no timestamps found")
    return min(timestamps), max(timestamps)


def _iter_log_timestamps(paths: Iterable[Path]) -> Iterable[float | None]:
    for path in paths:
        for row in read_jsonl(path):
            if "timestamp" in row:
                yield parse_timestamp(row["timestamp"])
            elif "start" in row:
                yield parse_timestamp(row["start"])
            elif "end" in row:
                yield parse_timestamp(row["end"])


def _run_paths_from_dir(config: MetricsConfig, path: Path) -> RunPaths:
    configuration_slug = path.parent.name
    return RunPaths(
        configuration_slug=configuration_slug,
        configuration=config.configuration_labels.get(
            configuration_slug,
            _title_from_slug(configuration_slug),
        ),
        run_id=path.name,
        path=path,
        requests_path=path / "requests.jsonl",
        responses_path=path / "responses.jsonl",
        prometheus_path=path / "prometheus_samples.jsonl",
    )


def _looks_like_run_dir(path: Path) -> bool:
    return (path / "requests.jsonl").exists() or (path / "responses.jsonl").exists()


def _title_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)
