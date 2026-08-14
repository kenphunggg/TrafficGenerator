"""Shared helpers for offline metrics run directories."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .metrics_config import MetricsConfig

AGGREGATE_DIR_NAMES = {"metrics", "plots"}


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
    run_id: str | None = None,
) -> list[RunPaths]:
    if run_dir is not None:
        runs = _discover_run_paths_from_explicit_dir(config, Path(run_dir))
        if run_id is not None:
            runs = [run for run in runs if run.run_id == run_id]
        return runs

    if not config.results_dir.exists():
        return []

    runs: list[RunPaths] = []
    for top_dir in sorted(config.results_dir.iterdir()):
        if not top_dir.is_dir() or top_dir.name in AGGREGATE_DIR_NAMES:
            continue

        # Current layout: results/<run_id>/<configuration>/
        if top_dir.name not in config.configuration_labels:
            for case_dir in sorted(top_dir.iterdir()):
                if case_dir.is_dir() and _looks_like_run_dir(case_dir):
                    run = _run_paths_from_dir(config, case_dir, layout="run-centric")
                    if run_id is not None and run.run_id != run_id:
                        continue
                    runs.append(run)
            continue

        # Legacy layout kept readable: results/<configuration>/<run_id>/
        for candidate in sorted(top_dir.iterdir()):
            if candidate.is_dir() and _looks_like_run_dir(candidate):
                run = _run_paths_from_dir(config, candidate, layout="configuration-centric")
                if run_id is not None and run.run_id != run_id:
                    continue
                runs.append(run)
    return runs


def case_run_dir(config: MetricsConfig, configuration_slug: str, run_id: str) -> Path:
    """Canonical run log directory: results/<run_id>/<configuration>/."""

    return config.results_dir / run_id / configuration_slug


def run_metrics_dir(config: MetricsConfig, run_id: str | None) -> Path:
    """Canonical comparison CSV directory for a shared run id."""

    if run_id:
        return config.results_dir / run_id / "metrics"
    return config.output_dir


def run_plots_dir(config: MetricsConfig, run_id: str | None) -> Path:
    """Canonical comparison plot directory for a shared run id."""

    if run_id:
        return config.results_dir / run_id / "plots"
    return config.plots_dir


def aggregate_output_dir(
    config: MetricsConfig,
    *,
    run_dir: str | Path | None,
    run_id: str | None,
) -> Path:
    if run_dir is None:
        return run_metrics_dir(config, run_id)
    path = Path(run_dir)
    if _looks_like_run_dir(path):
        return path
    return path / "metrics"


def plot_output_dir(
    config: MetricsConfig,
    *,
    run_dir: str | Path | None,
    run_id: str | None,
) -> Path:
    if run_dir is None:
        return run_plots_dir(config, run_id)
    path = Path(run_dir)
    if _looks_like_run_dir(path):
        return path
    return path / "plots"


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


def _discover_run_paths_from_explicit_dir(config: MetricsConfig, path: Path) -> list[RunPaths]:
    if _looks_like_run_dir(path):
        return [_run_paths_from_dir(config, path, layout=_infer_layout(config, path))]
    if not path.exists() or not path.is_dir():
        return []

    runs: list[RunPaths] = []
    for candidate in sorted(path.iterdir()):
        if candidate.is_dir() and _looks_like_run_dir(candidate):
            runs.append(_run_paths_from_dir(config, candidate, layout="run-centric"))
    return runs


def _run_paths_from_dir(
    config: MetricsConfig,
    path: Path,
    *,
    layout: str,
) -> RunPaths:
    if layout == "run-centric":
        configuration_slug = path.name
        run_id = path.parent.name
    elif layout == "configuration-centric":
        configuration_slug = path.parent.name
        run_id = path.name
    else:
        raise ValueError(f"unknown results layout: {layout}")
    return RunPaths(
        configuration_slug=configuration_slug,
        configuration=config.configuration_labels.get(
            configuration_slug,
            _title_from_slug(configuration_slug),
        ),
        run_id=run_id,
        path=path,
        requests_path=path / "requests.jsonl",
        responses_path=path / "responses.jsonl",
        prometheus_path=path / "prometheus_samples.jsonl",
    )


def _looks_like_run_dir(path: Path) -> bool:
    return (path / "requests.jsonl").exists() or (path / "responses.jsonl").exists()


def _infer_layout(config: MetricsConfig, path: Path) -> str:
    if path.name in config.configuration_labels:
        return "run-centric"
    return "configuration-centric"


def _title_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)
