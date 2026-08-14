"""Aggregate TrafficGenerator and Prometheus logs into plot-ready CSVs."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .metrics_config import MetricsConfig
from .metrics_runs import (
    RunPaths,
    aggregate_output_dir,
    discover_run_paths,
    parse_timestamp,
    read_jsonl,
)


SUMMARY_FIELDS = [
    "configuration",
    "run_id",
    "successful_requests",
    "total_requests",
    "allocated_core_seconds",
    "actual_core_seconds",
    "peak_allocated_cores",
    "warm_p95_ms",
    "cold_p95_ms",
    "slo_violation_pct",
    "timeout_pct",
    "allocated_core_seconds_per_1000_successful_requests",
]

TIMELINE_FIELDS = [
    "configuration",
    "run_id",
    "minute",
    "offered_rps",
    "completed_rps",
    "inflight",
    "warm_p95_ms",
    "ready_pods",
    "pending_pods",
    "allocated_cpu_cores",
    "actual_cpu_cores",
    "nimbus_mode",
    "nimbus_tier",
]

LATENCY_FIELDS = [
    "configuration",
    "run_id",
    "request_id",
    "state",
    "latency_ms",
    "success",
    "status_code",
    "timed_out",
    "pod_name",
]

NUMERIC_PROMETHEUS_METRICS = {
    "ready_pods",
    "pending_pods",
    "allocated_cpu_cores",
    "actual_cpu_cores",
}


@dataclass(frozen=True)
class RequestRecord:
    request_id: int
    scheduled_sec: float | None
    started_sec: float | None


@dataclass(frozen=True)
class ResponseRecord:
    request_id: int
    completed_sec: float | None
    latency_ms: float | None
    success: bool
    status_code: int | None
    timed_out: bool
    state: str
    pod_name: str


@dataclass(frozen=True)
class RunAggregation:
    latency_rows: list[dict[str, Any]]
    timeline_rows: list[dict[str, Any]]
    summary_row: dict[str, Any]


@dataclass(frozen=True)
class PrometheusSamples:
    numeric: dict[str, list[tuple[float, float]]]
    text: dict[str, list[tuple[float, str]]]

    def numeric_bucket(self, metric: str, start_sec: float, end_sec: float) -> float | None:
        values = [value for rel_sec, value in self.numeric.get(metric, []) if start_sec <= rel_sec < end_sec]
        if values:
            return sum(values) / len(values)
        previous = [value for rel_sec, value in self.numeric.get(metric, []) if rel_sec <= end_sec]
        return previous[-1] if previous else None

    def text_bucket(self, metric: str, start_sec: float, end_sec: float) -> str:
        values = [value for rel_sec, value in self.text.get(metric, []) if start_sec <= rel_sec < end_sec]
        if values:
            return values[-1]
        previous = [value for rel_sec, value in self.text.get(metric, []) if rel_sec <= end_sec]
        return previous[-1] if previous else ""


def aggregate_metrics(
    config: MetricsConfig,
    *,
    run_dir: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Path]:
    runs = discover_run_paths(config, run_dir=run_dir, run_id=run_id)
    output_dir = aggregate_output_dir(config, run_dir=run_dir, run_id=run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    aggregations = [aggregate_run(config, run) for run in runs]

    paths = {
        "latency_samples": output_dir / "latency_samples.csv",
        "timeline": output_dir / "timeline.csv",
        "summary_metrics": output_dir / "summary_metrics.csv",
    }
    _write_csv(
        paths["latency_samples"],
        LATENCY_FIELDS,
        [row for aggregation in aggregations for row in aggregation.latency_rows],
    )
    _write_csv(
        paths["timeline"],
        TIMELINE_FIELDS,
        [row for aggregation in aggregations for row in aggregation.timeline_rows],
    )
    _write_csv(
        paths["summary_metrics"],
        SUMMARY_FIELDS,
        [aggregation.summary_row for aggregation in aggregations],
    )
    return paths


def aggregate_run(config: MetricsConfig, run: RunPaths) -> RunAggregation:
    request_rows = read_jsonl(run.requests_path)
    response_rows = read_jsonl(run.responses_path)
    run_start_epoch = _run_start_epoch(request_rows, response_rows)

    requests = _parse_requests(request_rows, run_start_epoch)
    responses = _parse_responses(response_rows, run_start_epoch, config)
    prometheus = _load_prometheus_samples(run.prometheus_path, run_start_epoch)

    timeline_rows = _build_timeline_rows(config, run, requests, responses, prometheus)
    return RunAggregation(
        latency_rows=_build_latency_rows(run, responses),
        timeline_rows=timeline_rows,
        summary_row=_build_summary_row(config, run, requests, responses, timeline_rows),
    )


def _parse_requests(
    rows: list[dict[str, Any]],
    run_start_epoch: float | None,
) -> dict[int, RequestRecord]:
    parsed: dict[int, RequestRecord] = {}
    for row in rows:
        request_id = _request_id(row)
        if request_id is None:
            continue
        timestamp = parse_timestamp(row.get("timestamp")) if run_start_epoch is not None else None
        started_sec = None if timestamp is None or run_start_epoch is None else timestamp - run_start_epoch
        scheduled_sec = _optional_float(row.get("scheduled_at_sec"))
        parsed[request_id] = RequestRecord(
            request_id=request_id,
            scheduled_sec=scheduled_sec,
            started_sec=started_sec if started_sec is not None else scheduled_sec,
        )
    return parsed


def _parse_responses(
    rows: list[dict[str, Any]],
    run_start_epoch: float | None,
    config: MetricsConfig,
) -> dict[int, ResponseRecord]:
    parsed: dict[int, ResponseRecord] = {}
    timeout_ms = config.request_timeout_sec * 1000
    for row in rows:
        request_id = _request_id(row)
        if request_id is None:
            continue
        timestamp = parse_timestamp(row.get("timestamp")) if run_start_epoch is not None else None
        completed_sec = None if timestamp is None or run_start_epoch is None else timestamp - run_start_epoch
        raw_latency_ms = _optional_float(row.get("response_time_ms"))
        timed_out = _is_timeout(row, timeout_ms)
        latency_ms = min(raw_latency_ms, timeout_ms) if raw_latency_ms is not None and timed_out else raw_latency_ms
        if latency_ms is None and timed_out:
            latency_ms = timeout_ms
        parsed[request_id] = ResponseRecord(
            request_id=request_id,
            completed_sec=completed_sec,
            latency_ms=latency_ms,
            success=_boolish(row.get("success")),
            status_code=_optional_int(row.get("status_code")),
            timed_out=timed_out,
            state=_request_state(row),
            pod_name=str(row.get("pod_name", "") or ""),
        )
    return parsed


def _build_latency_rows(
    run: RunPaths,
    responses: dict[int, ResponseRecord],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for response in sorted(responses.values(), key=lambda item: item.request_id):
        if response.latency_ms is None:
            continue
        rows.append(
            {
                "configuration": run.configuration,
                "run_id": run.run_id,
                "request_id": response.request_id,
                "state": response.state,
                "latency_ms": _format_number(response.latency_ms),
                "success": str(response.success).lower(),
                "status_code": "" if response.status_code is None else response.status_code,
                "timed_out": str(response.timed_out).lower(),
                "pod_name": response.pod_name,
            }
        )
    return rows


def _build_timeline_rows(
    config: MetricsConfig,
    run: RunPaths,
    requests: dict[int, RequestRecord],
    responses: dict[int, ResponseRecord],
    prometheus: PrometheusSamples,
) -> list[dict[str, Any]]:
    step = config.step_sec
    max_sec = _max_observed_sec(requests, responses, prometheus)
    bucket_count = max(1, int(math.floor(max_sec / step)) + 1)
    rows: list[dict[str, Any]] = []

    for bucket in range(bucket_count):
        bucket_start = bucket * step
        bucket_end = bucket_start + step
        offered_count = sum(
            1
            for request in requests.values()
            if _in_bucket(_request_offer_sec(request), bucket_start, bucket_end)
        )
        completed = [
            response
            for response in responses.values()
            if response.success and _in_bucket(response.completed_sec, bucket_start, bucket_end)
        ]
        warm_success_latencies = [
            response.latency_ms
            for response in completed
            if response.state == "warm" and response.latency_ms is not None
        ]

        rows.append(
            {
                "configuration": run.configuration,
                "run_id": run.run_id,
                "minute": _format_number(bucket_start / 60),
                "offered_rps": _format_number(offered_count / step),
                "completed_rps": _format_number(len(completed) / step),
                "inflight": _format_number(_inflight_at(requests, responses, bucket_end)),
                "warm_p95_ms": _format_optional(_percentile(warm_success_latencies, 95)),
                "ready_pods": _format_optional(
                    prometheus.numeric_bucket("ready_pods", bucket_start, bucket_end)
                ),
                "pending_pods": _format_optional(
                    prometheus.numeric_bucket("pending_pods", bucket_start, bucket_end)
                ),
                "allocated_cpu_cores": _format_optional(
                    prometheus.numeric_bucket("allocated_cpu_cores", bucket_start, bucket_end)
                ),
                "actual_cpu_cores": _format_optional(
                    prometheus.numeric_bucket("actual_cpu_cores", bucket_start, bucket_end)
                ),
                "nimbus_mode": prometheus.text_bucket("nimbus_mode", bucket_start, bucket_end),
                "nimbus_tier": prometheus.text_bucket("nimbus_tier", bucket_start, bucket_end),
            }
        )
    return rows


def _build_summary_row(
    config: MetricsConfig,
    run: RunPaths,
    requests: dict[int, RequestRecord],
    responses: dict[int, ResponseRecord],
    timeline_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    total_requests = len(requests) if requests else len(responses)
    successful_responses = [response for response in responses.values() if response.success]
    warm_success = [
        response.latency_ms
        for response in successful_responses
        if response.state == "warm" and response.latency_ms is not None
    ]
    cold_success = [
        response.latency_ms
        for response in successful_responses
        if response.state == "cold" and response.latency_ms is not None
    ]

    slo_misses = _missing_response_count(requests, responses)
    timed_out = 0
    for response in responses.values():
        timed_out += 1 if response.timed_out else 0
        if _is_slo_miss(config, response):
            slo_misses += 1

    allocated_core_seconds = _integrate_timeline_metric(
        timeline_rows,
        "allocated_cpu_cores",
        config.step_sec,
    )
    actual_core_seconds = _integrate_timeline_metric(
        timeline_rows,
        "actual_cpu_cores",
        config.step_sec,
    )
    peak_allocated = _peak_timeline_metric(timeline_rows, "allocated_cpu_cores")
    allocated_per_1k = (
        allocated_core_seconds / len(successful_responses) * 1000
        if allocated_core_seconds is not None and successful_responses
        else None
    )

    return {
        "configuration": run.configuration,
        "run_id": run.run_id,
        "successful_requests": len(successful_responses),
        "total_requests": total_requests,
        "allocated_core_seconds": _format_optional(allocated_core_seconds),
        "actual_core_seconds": _format_optional(actual_core_seconds),
        "peak_allocated_cores": _format_optional(peak_allocated),
        "warm_p95_ms": _format_optional(_percentile(warm_success, 95)),
        "cold_p95_ms": _format_optional(_percentile(cold_success, 95)),
        "slo_violation_pct": _format_optional(_percentage(slo_misses, total_requests)),
        "timeout_pct": _format_optional(_percentage(timed_out, total_requests)),
        "allocated_core_seconds_per_1000_successful_requests": _format_optional(
            allocated_per_1k
        ),
    }


def _load_prometheus_samples(path: Path, run_start_epoch: float | None) -> PrometheusSamples:
    if run_start_epoch is None:
        return PrometheusSamples(numeric={}, text={})

    numeric: dict[str, list[tuple[float, float]]] = {}
    text: dict[str, list[tuple[float, str]]] = {}
    for row in read_jsonl(path):
        metric = str(row.get("metric", ""))
        if not metric:
            continue
        if "value" in row and "timestamp" in row:
            timestamp = parse_timestamp(row["timestamp"])
            if timestamp is None:
                continue
            if metric in NUMERIC_PROMETHEUS_METRICS:
                value = _optional_float(row.get("value"))
                if value is not None:
                    numeric.setdefault(metric, []).append((timestamp - run_start_epoch, value))
            else:
                text.setdefault(metric, []).append((timestamp - run_start_epoch, str(row["value"])))
            continue

        response = row.get("response")
        if not isinstance(response, dict):
            continue
        if metric in NUMERIC_PROMETHEUS_METRICS:
            for timestamp, value in _numeric_values_from_prometheus_response(response):
                numeric.setdefault(metric, []).append((timestamp - run_start_epoch, value))
        else:
            for timestamp, value in _label_values_from_prometheus_response(response, metric):
                text.setdefault(metric, []).append((timestamp - run_start_epoch, value))

    for values in numeric.values():
        values.sort(key=lambda item: item[0])
    for values in text.values():
        values.sort(key=lambda item: item[0])
    return PrometheusSamples(numeric=numeric, text=text)


def _numeric_values_from_prometheus_response(response: dict[str, Any]) -> list[tuple[float, float]]:
    totals: dict[float, float] = {}
    for result in _prometheus_results(response):
        for timestamp, value in _prometheus_result_values(result):
            totals[timestamp] = totals.get(timestamp, 0.0) + value
    return sorted(totals.items())


def _label_values_from_prometheus_response(
    response: dict[str, Any],
    metric_name: str,
) -> list[tuple[float, str]]:
    label_candidates = (
        ("mode", "nimbus_mode", "state")
        if metric_name == "nimbus_mode"
        else ("tier", "nimbus_tier", "decision")
    )
    selected: dict[float, tuple[float, str]] = {}
    for result in _prometheus_results(response):
        labels = result.get("metric", {})
        if not isinstance(labels, dict):
            continue
        label = ""
        for candidate in label_candidates:
            if labels.get(candidate):
                label = str(labels[candidate])
                break
        if not label:
            continue
        for timestamp, value in _prometheus_result_values(result):
            previous = selected.get(timestamp)
            if previous is None or value > previous[0]:
                selected[timestamp] = (value, label)
    return sorted((timestamp, label) for timestamp, (_, label) in selected.items())


def _prometheus_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data", {})
    if not isinstance(data, dict):
        return []
    result = data.get("result", [])
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def _prometheus_result_values(result: dict[str, Any]) -> list[tuple[float, float]]:
    raw_values = result.get("values")
    if raw_values is None and "value" in result:
        raw_values = [result["value"]]
    values: list[tuple[float, float]] = []
    if not isinstance(raw_values, list):
        return values
    for item in raw_values:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        timestamp = _optional_float(item[0])
        value = _optional_float(item[1])
        if timestamp is not None and value is not None and math.isfinite(value):
            values.append((timestamp, value))
    return values


def _run_start_epoch(
    request_rows: list[dict[str, Any]],
    response_rows: list[dict[str, Any]],
) -> float | None:
    timestamps = [
        parsed
        for row in request_rows + response_rows
        for parsed in [parse_timestamp(row.get("timestamp"))]
        if parsed is not None
    ]
    return min(timestamps) if timestamps else None


def _max_observed_sec(
    requests: dict[int, RequestRecord],
    responses: dict[int, ResponseRecord],
    prometheus: PrometheusSamples,
) -> float:
    values: list[float] = []
    values.extend(sec for request in requests.values() for sec in [_request_offer_sec(request)] if sec is not None)
    values.extend(sec for request in requests.values() for sec in [request.started_sec] if sec is not None)
    values.extend(sec for response in responses.values() for sec in [response.completed_sec] if sec is not None)
    for series in prometheus.numeric.values():
        values.extend(sec for sec, _ in series)
    for series in prometheus.text.values():
        values.extend(sec for sec, _ in series)
    return max(values) if values else 0.0


def _request_offer_sec(request: RequestRecord) -> float | None:
    return request.scheduled_sec if request.scheduled_sec is not None else request.started_sec


def _inflight_at(
    requests: dict[int, RequestRecord],
    responses: dict[int, ResponseRecord],
    at_sec: float,
) -> int:
    count = 0
    for request_id, request in requests.items():
        started_sec = request.started_sec
        if started_sec is None or started_sec > at_sec:
            continue
        response = responses.get(request_id)
        if response is None or response.completed_sec is None or response.completed_sec > at_sec:
            count += 1
    return count


def _integrate_timeline_metric(
    rows: list[dict[str, Any]],
    metric: str,
    step_sec: int,
) -> float | None:
    numeric = [
        value
        for value in (_optional_float(row.get(metric)) for row in rows)
        if value is not None
    ]
    if not numeric:
        return None
    return sum(value * step_sec for value in numeric)


def _peak_timeline_metric(rows: list[dict[str, Any]], metric: str) -> float | None:
    numeric = [
        value
        for value in (_optional_float(row.get(metric)) for row in rows)
        if value is not None
    ]
    return max(numeric) if numeric else None


def _missing_response_count(
    requests: dict[int, RequestRecord],
    responses: dict[int, ResponseRecord],
) -> int:
    if not requests:
        return 0
    return sum(1 for request_id in requests if request_id not in responses)


def _is_slo_miss(config: MetricsConfig, response: ResponseRecord) -> bool:
    if not response.success:
        return True
    if response.latency_ms is None:
        return True
    slo_ms = config.cold_slo_ms if response.state == "cold" else config.warm_slo_ms
    return response.latency_ms > slo_ms


def _request_state(row: dict[str, Any]) -> str:
    cold_start_time_ms = _optional_float(row.get("cold_start_time_ms"))
    if _boolish(row.get("cold_start")) or (cold_start_time_ms is not None and cold_start_time_ms > 0):
        return "cold"
    return "warm"


def _is_timeout(row: dict[str, Any], timeout_ms: float) -> bool:
    error = str(row.get("error", "") or "").lower()
    if "timeout" in error or "timed out" in error:
        return True
    latency_ms = _optional_float(row.get("response_time_ms"))
    return not _boolish(row.get("success")) and latency_ms is not None and latency_ms >= timeout_ms * 0.999


def _request_id(row: dict[str, Any]) -> int | None:
    value = row.get("request_id")
    if value is None or value == "":
        return None
    return int(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    return False


def _in_bucket(value: float | None, start_sec: float, end_sec: float) -> bool:
    return value is not None and start_sec <= value < end_sec


def _percentile(values: list[float], percentile: float) -> float | None:
    numeric = sorted(value for value in values if value is not None and math.isfinite(value))
    if not numeric:
        return None
    if len(numeric) == 1:
        return numeric[0]
    rank = (len(numeric) - 1) * percentile / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return numeric[int(rank)]
    weight = rank - lower
    return numeric[lower] * (1 - weight) + numeric[upper] * weight


def _percentage(part: int, total: int) -> float | None:
    if total <= 0:
        return None
    return part / total * 100


def _format_optional(value: float | None) -> str:
    return "" if value is None else _format_number(value)


def _format_number(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
