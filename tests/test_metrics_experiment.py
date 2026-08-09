from datetime import datetime

from traffic_generator.experiment_cli import (
    build_locust_command,
    estimate_alias_demands,
    timestamp_run_id,
)
from traffic_generator.models import ReplayConfig, RoutingConfig, TargetConfig, TraceConfig, TraceRow, TrafficConfig


def test_timestamp_run_id_uses_date_and_time():
    assert timestamp_run_id(datetime(2026, 7, 29, 15, 30, 12)) == "20260729-153012"


def test_build_locust_command_uses_absolute_config_path(tmp_path):
    config = tmp_path / "trafficgen.config.toml"
    config.write_text("[trace]\nfile = 'trace.csv'\n")

    command = build_locust_command(
        trafficgen_config=str(config),
        users=1,
        spawn_rate=1.0,
    )

    assert "--headless" in command
    assert "-u" in command
    assert "1" in command
    assert "--trafficgen-config" in command
    assert str(config.resolve()) in command


def test_estimate_alias_demands_uses_peak_scaled_rps_and_busy_time():
    config = ReplayConfig(
        traffic=TrafficConfig(scale=0.5),
        target=TargetConfig(service_base="measure-yolo"),
        routing=RoutingConfig(increase_service=True),
    )
    rows = [
        TraceRow(minute=0, function_id="yolo_x_cpu", count=100),
        TraceRow(minute=1, function_id="yolo_x_cpu", count=200),
    ]

    demands = estimate_alias_demands(rows, config, assumed_service_time_sec=30)

    assert len(demands) == 1
    demand = demands[0]
    assert demand.service_base == "measure-yolo"
    assert demand.max_original_rpm == 200
    assert demand.max_scaled_rpm == 100
    assert demand.peak_scaled_rps == 100 / 60
    assert demand.required_aliases == 50


def test_run_experiment_writes_outputs_when_locust_fails(tmp_path, monkeypatch):
    import argparse

    from traffic_generator import experiment_cli
    from traffic_generator.ksvc_aliases import KsvcAliasConfig
    from traffic_generator.metrics_config import MetricsConfig

    calls = []

    class Completed:
        returncode = 7

    args = argparse.Namespace(
        config="trafficgen.config.toml",
        configuration="full-nimbus",
        run_id="run-failed",
        users=1,
        spawn_rate=1.0,
        timeout_sec=30.0,
        assumed_service_time_sec=30.0,
        yes=True,
        verbose=False,
        no_color=True,
        skip_alias_prepare=True,
        skip_cluster_capacity_check=True,
        skip_plot=False,
        dry_run=False,
    )
    replay_config = ReplayConfig(
        trace=TraceConfig(file=tmp_path / "trace.csv", start_minute=0, end_minute=0),
        traffic=TrafficConfig(scale=1.0),
        target=TargetConfig(service_base="measure-yolo"),
        routing=RoutingConfig(increase_service=True),
    )
    metrics_config = MetricsConfig(results_dir=tmp_path / "results")

    monkeypatch.setattr(experiment_cli, "load_config", lambda _path: replay_config)
    monkeypatch.setattr(experiment_cli, "load_trace", lambda _path: [TraceRow(0, "yolo_x_cpu", 1)])
    monkeypatch.setattr(experiment_cli, "load_metrics_config", lambda _path: metrics_config)
    monkeypatch.setattr(experiment_cli, "load_ksvc_alias_config", lambda _path: KsvcAliasConfig(enabled=False))
    monkeypatch.setattr(experiment_cli, "build_locust_command", lambda **_kwargs: ["locust"] )
    monkeypatch.setattr(experiment_cli.subprocess, "run", lambda *_args, **_kwargs: Completed())
    monkeypatch.setattr(experiment_cli, "collect_prometheus_samples", lambda *_args, **_kwargs: calls.append("collect") or [])
    monkeypatch.setattr(
        experiment_cli,
        "aggregate_metrics",
        lambda *_args, **_kwargs: calls.append("aggregate") or {"summary_metrics": tmp_path / "summary_metrics.csv"},
    )
    monkeypatch.setattr(
        experiment_cli,
        "plot_metrics",
        lambda *_args, **_kwargs: calls.append("plot") or {"tradeoff": tmp_path / "plot.png"},
    )

    assert experiment_cli.run_experiment(args) == 7
    assert calls == ["collect", "aggregate", "plot"]
