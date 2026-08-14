"""Prometheus range-query collection for experiment runs."""

from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from .metrics_config import MetricsConfig
from .metrics_runs import RunPaths, discover_run_paths, run_time_window


def collect_prometheus_samples(
    config: MetricsConfig,
    *,
    run_dir: str | Path | None = None,
    run_id: str | None = None,
    timeout_sec: float = 30.0,
) -> list[Path]:
    if not config.prometheus_url:
        raise ValueError("prometheus.url is required for metrics collect")

    written: list[Path] = []
    for run in discover_run_paths(config, run_dir=run_dir, run_id=run_id):
        start, end = run_time_window(run)
        records = list(_collect_run(config, run, start=start, end=end, timeout_sec=timeout_sec))
        run.path.mkdir(parents=True, exist_ok=True)
        with run.prometheus_path.open("w") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        written.append(run.prometheus_path)
    return written


def query_range(
    prometheus_url: str,
    *,
    query: str,
    start: float,
    end: float,
    step_sec: int,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    params = urlencode(
        {
            "query": query,
            "start": f"{start:.3f}",
            "end": f"{end:.3f}",
            "step": str(step_sec),
        }
    )
    url = f"{prometheus_url.rstrip('/')}/api/v1/query_range?{params}"
    with urlopen(url, timeout=timeout_sec) as response:  # noqa: S310 - configured Prometheus URL.
        loaded = json.loads(response.read().decode("utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Prometheus response must be a JSON object")
    if loaded.get("status") != "success":
        raise ValueError(f"Prometheus query failed: {loaded}")
    return loaded


def render_promql(config: MetricsConfig, run: RunPaths, template: str) -> str:
    if not template.strip():
        return ""
    return Template(template).safe_substitute(
        {
            "namespace": config.namespace,
            "service_base": config.service_base,
            "service_regex": config.service_regex,
            "configuration": run.configuration,
            "configuration_slug": run.configuration_slug,
            "run_id": run.run_id,
        }
    )


def _collect_run(
    config: MetricsConfig,
    run: RunPaths,
    *,
    start: float,
    end: float,
    timeout_sec: float,
):
    for metric, template in sorted(config.promql.items()):
        query = render_promql(config, run, template)
        if not query:
            continue
        response = query_range(
            config.prometheus_url or "",
            query=query,
            start=start,
            end=end,
            step_sec=config.step_sec,
            timeout_sec=timeout_sec,
        )
        yield {
            "metric": metric,
            "query": query,
            "start": start,
            "end": end,
            "step_sec": config.step_sec,
            "configuration": run.configuration,
            "configuration_slug": run.configuration_slug,
            "run_id": run.run_id,
            "response": response,
        }
