from traffic_generator.metrics_config import MetricsConfig
from traffic_generator.metrics_runs import (
    aggregate_output_dir,
    case_run_dir,
    plot_output_dir,
    run_metrics_dir,
    run_plots_dir,
)


def test_shared_run_layout_uses_case_metrics_and_plots_dirs(tmp_path):
    config = MetricsConfig(results_dir=tmp_path / "results")

    assert case_run_dir(config, "static-knative", "rep-02") == (
        tmp_path / "results" / "rep-02" / "static-knative"
    )
    assert run_metrics_dir(config, "rep-02") == tmp_path / "results" / "rep-02" / "metrics"
    assert run_plots_dir(config, "rep-02") == tmp_path / "results" / "rep-02" / "plots"


def test_explicit_shared_run_dir_writes_nested_artifact_dirs(tmp_path):
    config = MetricsConfig(results_dir=tmp_path / "results")
    run_dir = tmp_path / "results" / "rep-02"

    assert aggregate_output_dir(config, run_dir=run_dir, run_id="rep-02") == (
        tmp_path / "results" / "rep-02" / "metrics"
    )
    assert plot_output_dir(config, run_dir=run_dir, run_id="rep-02") == (
        tmp_path / "results" / "rep-02" / "plots"
    )


def test_explicit_case_run_dir_keeps_per_case_outputs_in_place(tmp_path):
    config = MetricsConfig(results_dir=tmp_path / "results")
    case_dir = tmp_path / "results" / "rep-02" / "full-nimbus"
    case_dir.mkdir(parents=True)
    (case_dir / "requests.jsonl").write_text("")

    assert aggregate_output_dir(config, run_dir=case_dir, run_id="rep-02") == case_dir
    assert plot_output_dir(config, run_dir=case_dir, run_id="rep-02") == case_dir
