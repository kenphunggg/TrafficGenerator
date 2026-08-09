from traffic_generator.ksvc_aliases import (
    all_ksvc_aliases_exist,
    build_check_ksvc_aliases_command,
    build_generate_ksvc_command,
    expected_ksvc_alias_names,
    load_ksvc_alias_config,
    with_ksvc_alias_count,
)


def test_load_ksvc_alias_config_from_central_config(tmp_path):
    config_file = tmp_path / "trafficgen.config.toml"
    config_file.write_text(
        """
[target]
service_base = "measure-yolo"
namespace = "serverless"

[routing]
suffix_template = "{service_base}-{index:03d}"

[ksvc_aliases]
enabled = true
template = "generateKsvc/templates/measure-yolo.yaml"
output_dir = "generated-ksvc"
pull_images = true
nodes = "generateKsvc/nodes.json"
create_namespace = true
apply = true
""".strip()
    )

    config = load_ksvc_alias_config(config_file)

    assert config.enabled is True
    assert config.count == 0
    assert config.template == tmp_path / "generateKsvc/templates/measure-yolo.yaml"
    assert config.output_dir == tmp_path / "generated-ksvc"
    assert config.base_name == "measure-yolo"
    assert config.suffix_template == "{base}-{index:03d}"
    assert config.namespace == "serverless"
    assert config.nodes == tmp_path / "generateKsvc/nodes.json"
    assert config.pull_images is True
    assert config.create_namespace is True
    assert config.apply is True


def test_build_generate_ksvc_command_uses_computed_count(tmp_path):
    config_file = tmp_path / "trafficgen.config.toml"
    config_file.write_text(
        """
[target]
service_base = "measure-yolo"

[ksvc_aliases]
enabled = true
template = "template.yaml"
output_dir = "generated-ksvc"
""".strip()
    )

    config = with_ksvc_alias_count(load_ksvc_alias_config(config_file), 5)
    command = build_generate_ksvc_command(config)

    assert "--count" in command
    assert "5" in command
    assert "--template" in command
    assert str(tmp_path / "template.yaml") in command


def test_expected_ksvc_alias_names_match_suffix_template(tmp_path):
    config_file = tmp_path / "trafficgen.config.toml"
    config_file.write_text(
        """
[target]
service_base = "measure-yolo"
namespace = "serverless"

[routing]
suffix_template = "{service_base}-{index:03d}"

[ksvc_aliases]
enabled = true
template = "template.yaml"
apply = true
""".strip()
    )

    config = with_ksvc_alias_count(load_ksvc_alias_config(config_file), 3)

    assert expected_ksvc_alias_names(config) == [
        "measure-yolo-001",
        "measure-yolo-002",
        "measure-yolo-003",
    ]
    assert build_check_ksvc_aliases_command(config) == [
        "kubectl",
        "get",
        "ksvc",
        "-n",
        "serverless",
        "measure-yolo-001",
        "measure-yolo-002",
        "measure-yolo-003",
    ]


def test_all_ksvc_aliases_exist_uses_kubectl_status(tmp_path, monkeypatch):
    config_file = tmp_path / "trafficgen.config.toml"
    config_file.write_text(
        """
[target]
service_base = "measure-yolo"
namespace = "serverless"

[ksvc_aliases]
enabled = true
template = "template.yaml"
apply = true
""".strip()
    )
    config = with_ksvc_alias_count(load_ksvc_alias_config(config_file), 1)
    calls = []

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr("traffic_generator.ksvc_aliases.subprocess.run", fake_run)

    assert all_ksvc_aliases_exist(config) is True
    assert calls[0][0] == [
        "kubectl",
        "get",
        "ksvc",
        "-n",
        "serverless",
        "measure-yolo-001",
    ]
