#!/usr/bin/env python3
"""Plot TrafficGenerator datatraces by day."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


MINUTES_PER_DAY = 24 * 60
DEFAULT_TRACES = ("day_night", "non_station")
DAY_TICKS = [0, 360, 720, 1080, 1440]
DAY_TICK_LABELS = ["00:00", "06:00", "12:00", "18:00", "24:00"]
OVERVIEW_DAY_TICKS = [day * MINUTES_PER_DAY for day in range(0, 32, 2)]
OVERVIEW_DAY_LABELS = [f"D{day + 1}" for day in range(0, 32, 2)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot TrafficGenerator datatrace CSV files into daily PNGs."
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Multiply counts before plotting. Default: 1.0",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Directory containing day_night.csv and non_station.csv.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "plot",
        help="Directory where plot/<trace_name>/ images are written.",
    )
    parser.add_argument(
        "--trace",
        action="append",
        choices=DEFAULT_TRACES,
        help="Trace to plot. Repeat to choose multiple. Default: both traces.",
    )
    parser.add_argument(
        "--expected-days",
        type=int,
        default=31,
        help="Expected number of days in each trace. Default: 31.",
    )
    return parser.parse_args()


def load_minute_counts(path: Path) -> dict[int, float]:
    if not path.exists():
        raise FileNotFoundError(f"trace file not found: {path}")

    counts: defaultdict[int, float] = defaultdict(float)
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {path}")

        fields = {field.strip(): field for field in reader.fieldnames if field}
        minute_col = fields.get("minute")
        count_col = fields.get("count") or fields.get("requests_per_minute")
        if minute_col is None:
            raise ValueError(f"{path} is missing required column: minute")
        if count_col is None:
            raise ValueError(f"{path} is missing count column")

        for row_no, row in enumerate(reader, start=2):
            try:
                minute = int((row.get(minute_col) or "").strip())
                count = float((row.get(count_col) or "").strip())
            except ValueError as exc:
                raise ValueError(f"{path}:{row_no}: invalid minute/count") from exc
            if minute < 0:
                raise ValueError(f"{path}:{row_no}: minute must be >= 0")
            if count < 0:
                raise ValueError(f"{path}:{row_no}: count must be >= 0")
            counts[minute] += count

    if not counts:
        raise ValueError(f"empty trace data: {path}")
    return dict(counts)


def split_days(
    minute_counts: dict[int, float],
    *,
    scale: float,
    expected_days: int,
) -> list[list[float]]:
    max_minute = max(minute_counts)
    observed_days = max(expected_days, math.ceil((max_minute + 1) / MINUTES_PER_DAY))
    days: list[list[float]] = []
    for day_index in range(observed_days):
        day_start = day_index * MINUTES_PER_DAY
        days.append(
            [
                minute_counts.get(day_start + minute_of_day, 0.0) * scale
                for minute_of_day in range(MINUTES_PER_DAY)
            ]
        )
    return days


def flatten_days(days: list[list[float]]) -> list[float]:
    return [count for day in days for count in day]


def plot_overview(
    days: list[list[float]],
    *,
    trace_name: str,
    scale: float,
    output_path: Path,
) -> None:
    values = flatten_days(days)
    y_max = y_axis_max(days)
    fig, ax = plt.subplots(figsize=(22, 5.8))
    ax.plot(range(len(values)), values, color="#1f77b4", linewidth=0.65)
    ax.set_title(f"{trace_name} - 31 Day Timeline (scale {scale:g})")
    ax.set_xlabel("full timeline, per minute")
    ax.set_ylabel("count per minute")
    ax.set_xlim(0, len(values) - 1)
    ax.set_ylim(0, y_max)
    ticks = [tick for tick in OVERVIEW_DAY_TICKS if tick < len(values)]
    labels = OVERVIEW_DAY_LABELS[: len(ticks)]
    if ticks[-1] != len(values) - 1:
        ticks.append(len(values) - 1)
        labels.append(f"D{len(days)} end")
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.grid(True, color="#d9d9d9", linewidth=0.55, alpha=0.85)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def plot_day(
    values: list[float],
    *,
    trace_name: str,
    day_index: int,
    scale: float,
    y_max: float,
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(13, 4.8))
    ax.plot(range(MINUTES_PER_DAY), values, color="#1f77b4", linewidth=1.05)
    ax.set_title(f"{trace_name} - Day {day_index + 1} (scale {scale:g})")
    ax.set_xlabel("time of day, per minute")
    ax.set_ylabel("count per minute")
    ax.set_xlim(0, MINUTES_PER_DAY - 1)
    ax.set_ylim(0, y_max)
    ax.set_xticks(DAY_TICKS)
    ax.set_xticklabels(DAY_TICK_LABELS)
    ax.grid(True, color="#d9d9d9", linewidth=0.55, alpha=0.85)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def y_axis_max(days: list[list[float]]) -> float:
    max_value = max((max(day) for day in days), default=0.0)
    if max_value <= 0:
        return 1.0
    return max_value * 1.08


def scale_label(scale: float) -> str:
    return f"{scale:g}".replace("-", "neg").replace(".", "p")


def plot_trace(
    trace_name: str,
    *,
    input_dir: Path,
    output_dir: Path,
    scale: float,
    expected_days: int,
) -> list[Path]:
    trace_path = input_dir / f"{trace_name}.csv"
    minute_counts = load_minute_counts(trace_path)
    days = split_days(minute_counts, scale=scale, expected_days=expected_days)
    trace_output_dir = output_dir / trace_name
    suffix = f"scale_{scale_label(scale)}"
    written: list[Path] = []

    overview_path = trace_output_dir / f"overview_31_days_{suffix}.png"
    plot_overview(days, trace_name=trace_name, scale=scale, output_path=overview_path)
    written.append(overview_path)

    y_max = y_axis_max(days)
    for day_index, values in enumerate(days):
        day_path = trace_output_dir / f"day_{day_index + 1:02d}_{suffix}.png"
        plot_day(
            values,
            trace_name=trace_name,
            day_index=day_index,
            scale=scale,
            y_max=y_max,
            output_path=day_path,
        )
        written.append(day_path)

    return written


def main() -> None:
    args = parse_args()
    if args.scale < 0:
        raise SystemExit("--scale must be >= 0")
    if args.expected_days <= 0:
        raise SystemExit("--expected-days must be > 0")

    traces = tuple(args.trace or DEFAULT_TRACES)
    written_count = 0
    for trace_name in traces:
        written = plot_trace(
            trace_name,
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            scale=args.scale,
            expected_days=args.expected_days,
        )
        written_count += len(written)
        print(f"{trace_name}: wrote {len(written)} plots to {args.output_dir / trace_name}")
    print(f"wrote {written_count} plots total")


if __name__ == "__main__":
    main()
