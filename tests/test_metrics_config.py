from traffic_generator.metrics_config import load_metrics_config


def test_load_metrics_config_from_toml(tmp_path):
    config_file = tmp_path / "trafficgen.config.toml"
    config_file.write_text(
        """
[target]
service_base = "measure-yolo"
namespace = "serverless-test"

[metrics]
results_dir = "my-results"
step_sec = 30
warm_slo_ms = 1200

[metrics.configuration_labels]
custom = "Custom Config"

[metrics.prometheus]
url = "http://prometheus.example"
service_regex = "measure-.*"

[metrics.prometheus.queries]
ready_pods = "custom_ready"
""".strip()
    )

    config = load_metrics_config(config_file)

    assert config.results_dir == tmp_path / "my-results"
    assert config.step_sec == 30
    assert config.warm_slo_ms == 1200
    assert config.prometheus_url == "http://prometheus.example"
    assert config.namespace == "serverless-test"
    assert config.service_regex == "measure-.*"
    assert config.configuration_labels["custom"] == "Custom Config"
    assert config.promql["ready_pods"] == "custom_ready"


def test_load_metrics_config_from_central_traffic_config(tmp_path):
    config_file = tmp_path / "trafficgen.config.toml"
    config_file.write_text(
        """
[target]
service_base = "measure-yolo"
namespace = "serverless"

[metrics]
results_dir = "results"
step_sec = 15

[metrics.prometheus]
url = "http://prometheus.example"

[metrics.prometheus.queries]
actual_cpu_cores = "custom_cpu"
""".strip()
    )

    config = load_metrics_config(config_file)

    assert config.results_dir == tmp_path / "results"
    assert config.step_sec == 15
    assert config.prometheus_url == "http://prometheus.example"
    assert config.namespace == "serverless"
    assert config.service_base == "measure-yolo"
    assert config.service_regex == "measure-yolo.*"
    assert config.promql["actual_cpu_cores"] == "custom_cpu"


def test_load_metrics_config_defaults_from_services_and_cluster(tmp_path):
    config_file = tmp_path / "trafficgen.config.toml"
    config_file.write_text(
        """
[cluster]
namespace = "serverless"

[[services]]
service_base = "measure-yolo"

[metrics]
results_dir = "results"

[metrics.prometheus]
url = "http://prometheus.example"
""".strip()
    )

    config = load_metrics_config(config_file)

    assert config.namespace == "serverless"
    assert config.service_base == "measure-yolo"
    assert config.service_regex == "measure-yolo.*"
