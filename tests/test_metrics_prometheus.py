from traffic_generator.metrics_config import MetricsConfig
from traffic_generator.metrics_prometheus import render_promql
from traffic_generator.metrics_runs import RunPaths


def test_render_promql_uses_config_and_run_values(tmp_path):
    config = MetricsConfig(namespace="ns", service_base="svc", service_regex="svc-.*")
    run = RunPaths(
        configuration_slug="full-nimbus",
        configuration="Full Nimbus",
        run_id="run-01",
        path=tmp_path,
        requests_path=tmp_path / "requests.jsonl",
        responses_path=tmp_path / "responses.jsonl",
        prometheus_path=tmp_path / "prometheus_samples.jsonl",
    )

    rendered = render_promql(
        config,
        run,
        'sum(my_metric{namespace="$namespace",pod=~"$service_regex",run="$run_id"})',
    )

    assert rendered == 'sum(my_metric{namespace="ns",pod=~"svc-.*",run="run-01"})'
