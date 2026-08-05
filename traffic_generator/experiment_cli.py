"""One-command experiment runner for traffic replay and metrics output."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .ksvc_aliases import (
    all_ksvc_aliases_exist,
    build_generate_ksvc_command,
    load_ksvc_alias_config,
)
from .metrics_aggregate import aggregate_metrics
from .metrics_config import load_metrics_config
from .metrics_plots import plot_metrics
from .metrics_prometheus import collect_prometheus_samples


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run traffic and build metrics/plots")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Prepare aliases, run Locust, collect metrics, and plot")
    run.add_argument(
        "--config",
        default=None,
        help="Central trafficgen.config.toml containing replay, metrics, and alias settings",
    )
    run.add_argument(
        "--configuration",
        required=True,
        help="Result folder slug, for example static-knative, fixed-boost, or full-nimbus",
    )
    run.add_argument(
        "--run-id",
        help="Run folder name. Defaults to a timestamp such as 20260729-153012.",
    )
    run.add_argument("--users", "-u", type=int, default=1, help="Locust user count")
    run.add_argument("--spawn-rate", "-r", type=float, default=1.0, help="Locust spawn rate")
    run.add_argument("--timeout-sec", type=float, default=30.0, help="Prometheus HTTP timeout")
    run.add_argument("--skip-alias-prepare", action="store_true", help="Do not run generateKsvc even if ksvc_aliases.enabled = true")
    run.add_argument("--skip-plot", action="store_true", help="Collect and aggregate, but do not plot")
    run.add_argument("--dry-run", action="store_true", help="Print the Locust command without running it")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return run_experiment(args)
    raise AssertionError(f"unknown experiment command: {args.command}")


def run_experiment(args: argparse.Namespace) -> int:
    central_config = args.config or "trafficgen.config.toml"

    metrics_config = load_metrics_config(central_config)
    alias_config = load_ksvc_alias_config(central_config)
    alias_command = (
        []
        if args.skip_alias_prepare or not alias_config.enabled
        else build_generate_ksvc_command(alias_config)
    )
    run_id = args.run_id or timestamp_run_id()
    run_dir = (metrics_config.results_dir / args.configuration / run_id).resolve()

    locust_command = build_locust_command(
        trafficgen_config=central_config,
        users=args.users,
        spawn_rate=args.spawn_rate,
    )
    env = os.environ.copy()
    env["LOG_DIR"] = str(run_dir)

    print(f"configuration={args.configuration}")
    print(f"run_id={run_id}")
    print(f"run_dir={run_dir}")
    if alias_command:
        print("preparing KService aliases:")
        print("  " + " ".join(alias_command))
    print("running traffic:")
    print("  " + " ".join(locust_command))

    if args.dry_run:
        return 0

    if alias_command:
        if all_ksvc_aliases_exist(alias_config):
            print("KService aliases already exist; skipping alias preparation")
        else:
            completed = subprocess.run(alias_command, check=False)  # noqa: S603 - fixed executable/args.
            if completed.returncode != 0:
                return completed.returncode

    run_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(locust_command, env=env, check=False)  # noqa: S603 - fixed executable/args.
    if completed.returncode != 0:
        return completed.returncode

    sample_paths = collect_prometheus_samples(
        metrics_config,
        run_dir=run_dir,
        timeout_sec=args.timeout_sec,
    )
    _print_paths("wrote Prometheus samples", sample_paths)
    csv_paths = aggregate_metrics(metrics_config, run_dir=run_dir)
    _print_paths("wrote aggregate CSVs", csv_paths.values())
    if not args.skip_plot:
        plot_paths = plot_metrics(
            metrics_config,
            input_dir=run_dir,
            output_dir=run_dir,
            run_id=run_id,
        )
        _print_paths("wrote plots", plot_paths.values())
    return 0


def timestamp_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def build_locust_command(
    *,
    trafficgen_config: str,
    users: int,
    spawn_rate: float,
) -> list[str]:
    locust = _locust_executable()
    locustfile = Path(__file__).resolve().parent / "locustfile.py"
    return [
        locust,
        "-f",
        str(locustfile),
        "--headless",
        "-u",
        str(users),
        "-r",
        _format_number(spawn_rate),
        "--trafficgen-config",
        str(Path(trafficgen_config).resolve()),
    ]


def _locust_executable() -> str:
    sibling = Path(sys.executable).with_name("locust")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("locust")
    if found:
        return found
    raise FileNotFoundError("locust executable not found")


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _print_paths(message: str, paths) -> None:
    normalized = [Path(path) for path in paths]
    if not normalized:
        print(f"{message}: none")
        return
    print(f"{message}:")
    for path in normalized:
        print(f"  {path}")
