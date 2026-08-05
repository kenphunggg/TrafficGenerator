"""CLI entry points for offline metrics collection, aggregation, and plotting."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .metrics_aggregate import aggregate_metrics
from .metrics_config import load_metrics_config
from .metrics_plots import plot_metrics
from .metrics_prometheus import collect_prometheus_samples


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TrafficGenerator metrics utilities")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to trafficgen.config.toml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Collect Prometheus samples for run dirs")
    collect.add_argument("--config", dest="command_config", help="Path to trafficgen.config.toml")
    collect.add_argument("--run-dir", help="Collect only this run directory")
    collect.add_argument("--timeout-sec", type=float, default=30.0, help="Prometheus HTTP timeout")

    aggregate = subparsers.add_parser("aggregate", help="Aggregate logs and Prometheus samples into CSVs")
    aggregate.add_argument("--config", dest="command_config", help="Path to trafficgen.config.toml")
    aggregate.add_argument("--run-dir", help="Aggregate only this run directory")

    plot = subparsers.add_parser("plot", help="Create thesis plots from aggregate CSVs")
    plot.add_argument("--config", dest="command_config", help="Path to trafficgen.config.toml")
    plot.add_argument("--input", help="Directory containing summary/timeline/latency CSVs")
    plot.add_argument("--output", help="Directory for generated PNG files")
    plot.add_argument("--run-id", help="Run ID for representative timeline/distribution plots")

    all_cmd = subparsers.add_parser("all", help="Collect Prometheus, aggregate CSVs, and plot")
    all_cmd.add_argument("--config", dest="command_config", help="Path to trafficgen.config.toml")
    all_cmd.add_argument("--run-dir", help="Process only this run directory")
    all_cmd.add_argument("--timeout-sec", type=float, default=30.0, help="Prometheus HTTP timeout")
    all_cmd.add_argument("--run-id", help="Run ID for representative timeline/distribution plots")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.command_config or args.config
    config = load_metrics_config(config_path)

    if args.command == "collect":
        paths = collect_prometheus_samples(
            config,
            run_dir=args.run_dir,
            timeout_sec=args.timeout_sec,
        )
        _print_paths("wrote Prometheus samples", paths)
        return 0

    if args.command == "aggregate":
        paths = aggregate_metrics(config, run_dir=args.run_dir)
        _print_paths("wrote aggregate CSVs", paths.values())
        return 0

    if args.command == "plot":
        paths = plot_metrics(
            config,
            input_dir=args.input,
            output_dir=args.output,
            run_id=args.run_id,
        )
        _print_paths("wrote plots", paths.values())
        return 0

    if args.command == "all":
        sample_paths = collect_prometheus_samples(
            config,
            run_dir=args.run_dir,
            timeout_sec=args.timeout_sec,
        )
        _print_paths("wrote Prometheus samples", sample_paths)
        csv_paths = aggregate_metrics(config, run_dir=args.run_dir)
        _print_paths("wrote aggregate CSVs", csv_paths.values())
        plot_input_dir = args.run_dir if args.run_dir is not None else None
        plot_output_dir = args.run_dir if args.run_dir is not None else None
        plot_paths = plot_metrics(
            config,
            input_dir=plot_input_dir,
            output_dir=plot_output_dir,
            run_id=args.run_id,
        )
        _print_paths("wrote plots", plot_paths.values())
        return 0

    raise AssertionError(f"unknown metrics command: {args.command}")


def _print_paths(message: str, paths) -> None:
    normalized = [Path(path) for path in paths]
    if not normalized:
        print(f"{message}: none")
        return
    print(f"{message}:")
    for path in normalized:
        print(f"  {path}")
