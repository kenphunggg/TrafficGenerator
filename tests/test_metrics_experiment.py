from datetime import datetime

from traffic_generator.experiment_cli import build_locust_command, timestamp_run_id


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
