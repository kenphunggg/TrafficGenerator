import csv
import json

from traffic_generator.metrics_aggregate import aggregate_metrics
from traffic_generator.metrics_config import MetricsConfig


def test_aggregate_existing_logs_and_prometheus_samples(tmp_path):
    run_dir = tmp_path / "results" / "full-nimbus" / "run-01"
    run_dir.mkdir(parents=True)
    _write_jsonl(
        run_dir / "requests.jsonl",
        [
            {
                "timestamp": "2026-07-23T00:00:00Z",
                "request_id": 1,
                "scheduled_at_sec": 0,
            },
            {
                "timestamp": "2026-07-23T00:00:10Z",
                "request_id": 2,
                "scheduled_at_sec": 10,
            },
            {
                "timestamp": "2026-07-23T00:01:10Z",
                "request_id": 3,
                "scheduled_at_sec": 70,
            },
        ],
    )
    _write_jsonl(
        run_dir / "responses.jsonl",
        [
            {
                "timestamp": "2026-07-23T00:00:02Z",
                "request_id": 1,
                "success": True,
                "status_code": 200,
                "response_time_ms": 2000,
                "cold_start": True,
                "cold_start_time_ms": 1500,
                "pod_name": "pod-a",
            },
            {
                "timestamp": "2026-07-23T00:00:11Z",
                "request_id": 2,
                "success": True,
                "status_code": 200,
                "response_time_ms": 300,
                "cold_start": False,
                "cold_start_time_ms": 0,
                "pod_name": "pod-a",
            },
            {
                "timestamp": "2026-07-23T00:01:12Z",
                "request_id": 3,
                "success": False,
                "status_code": None,
                "response_time_ms": 300000,
                "error": "request timeout",
            },
        ],
    )
    _write_jsonl(
        run_dir / "prometheus_samples.jsonl",
        [
            {"metric": "ready_pods", "timestamp": "2026-07-23T00:00:00Z", "value": 1},
            {"metric": "ready_pods", "timestamp": "2026-07-23T00:01:00Z", "value": 2},
            {"metric": "pending_pods", "timestamp": "2026-07-23T00:00:00Z", "value": 0},
            {"metric": "allocated_cpu_cores", "timestamp": "2026-07-23T00:00:00Z", "value": 1.5},
            {"metric": "allocated_cpu_cores", "timestamp": "2026-07-23T00:01:00Z", "value": 2.0},
            {"metric": "actual_cpu_cores", "timestamp": "2026-07-23T00:00:00Z", "value": 0.7},
            {"metric": "actual_cpu_cores", "timestamp": "2026-07-23T00:01:00Z", "value": 0.8},
        ],
    )
    config = MetricsConfig(
        results_dir=tmp_path / "results",
        output_dir=tmp_path / "out",
        request_timeout_sec=300,
    )

    paths = aggregate_metrics(config)

    latency_rows = _read_csv(paths["latency_samples"])
    assert latency_rows[0]["state"] == "cold"
    assert latency_rows[1]["state"] == "warm"
    assert latency_rows[2]["timed_out"] == "true"

    timeline_rows = _read_csv(paths["timeline"])
    assert timeline_rows[0]["offered_rps"] == "0.033333"
    assert timeline_rows[0]["completed_rps"] == "0.033333"
    assert timeline_rows[0]["warm_p95_ms"] == "300"
    assert timeline_rows[1]["ready_pods"] == "2"

    summary_rows = _read_csv(paths["summary_metrics"])
    assert summary_rows[0]["configuration"] == "Full Nimbus"
    assert summary_rows[0]["successful_requests"] == "2"
    assert summary_rows[0]["total_requests"] == "3"
    assert summary_rows[0]["timeout_pct"] == "33.333333"
    assert summary_rows[0]["allocated_core_seconds"] == "210"
    assert summary_rows[0]["actual_core_seconds"] == "90"


def _write_jsonl(path, rows):
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _read_csv(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_aggregate_with_run_dir_writes_csvs_into_that_run_folder(tmp_path):
    run_dir = tmp_path / "results" / "full-nimbus" / "run-01"
    run_dir.mkdir(parents=True)
    _write_jsonl(
        run_dir / "requests.jsonl",
        [{"timestamp": "2026-07-23T00:00:00Z", "request_id": 1, "scheduled_at_sec": 0}],
    )
    _write_jsonl(
        run_dir / "responses.jsonl",
        [
            {
                "timestamp": "2026-07-23T00:00:01Z",
                "request_id": 1,
                "success": True,
                "status_code": 200,
                "response_time_ms": 100,
            }
        ],
    )
    _write_jsonl(run_dir / "prometheus_samples.jsonl", [])
    config = MetricsConfig(results_dir=tmp_path / "results", output_dir=tmp_path / "out")

    paths = aggregate_metrics(config, run_dir=run_dir)

    assert paths["latency_samples"] == run_dir / "latency_samples.csv"
    assert paths["timeline"] == run_dir / "timeline.csv"
    assert paths["summary_metrics"] == run_dir / "summary_metrics.csv"
    assert not (tmp_path / "out" / "summary_metrics.csv").exists()
    assert _read_csv(run_dir / "summary_metrics.csv")[0]["run_id"] == "run-01"

