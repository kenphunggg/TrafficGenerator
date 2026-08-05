import csv

from traffic_generator.metrics_config import MetricsConfig
from traffic_generator.metrics_plots import plot_metrics


def test_plot_metrics_creates_pngs(tmp_path):
    input_dir = tmp_path / "metrics"
    output_dir = tmp_path / "plots"
    input_dir.mkdir()
    _write_csv(
        input_dir / "summary_metrics.csv",
        [
            {
                "configuration": "Full Nimbus",
                "run_id": "run-01",
                "successful_requests": "2",
                "total_requests": "2",
                "allocated_core_seconds": "120",
                "actual_core_seconds": "60",
                "peak_allocated_cores": "2",
                "warm_p95_ms": "400",
                "cold_p95_ms": "2000",
                "slo_violation_pct": "0",
                "timeout_pct": "0",
                "allocated_core_seconds_per_1000_successful_requests": "60000",
            }
        ],
    )
    _write_csv(
        input_dir / "timeline.csv",
        [
            {
                "configuration": "Full Nimbus",
                "run_id": "run-01",
                "minute": "0",
                "offered_rps": "0.03",
                "completed_rps": "0.03",
                "inflight": "0",
                "warm_p95_ms": "400",
                "ready_pods": "1",
                "pending_pods": "0",
                "allocated_cpu_cores": "2",
                "actual_cpu_cores": "1",
                "nimbus_mode": "",
                "nimbus_tier": "",
            }
        ],
    )
    _write_csv(
        input_dir / "latency_samples.csv",
        [
            {
                "configuration": "Full Nimbus",
                "run_id": "run-01",
                "request_id": "1",
                "state": "warm",
                "latency_ms": "400",
                "success": "true",
                "status_code": "200",
                "timed_out": "false",
                "pod_name": "pod-a",
            },
            {
                "configuration": "Full Nimbus",
                "run_id": "run-01",
                "request_id": "2",
                "state": "cold",
                "latency_ms": "2000",
                "success": "true",
                "status_code": "200",
                "timed_out": "false",
                "pod_name": "pod-b",
            },
        ],
    )

    paths = plot_metrics(
        MetricsConfig(output_dir=input_dir, plots_dir=output_dir),
        input_dir=input_dir,
        output_dir=output_dir,
    )

    for path in paths.values():
        assert path.exists()
        assert path.stat().st_size > 0


def _write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
