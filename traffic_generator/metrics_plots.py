"""Plot real Nimbus experiment CSVs."""

from __future__ import annotations

import csv
import math
import random
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .metrics_config import MetricsConfig


CONFIG_COLORS = {
    "Static Knative": "#6b7280",
    "Fixed Startup Boost": "#2563eb",
    "Full Nimbus": "#dc2626",
}


def plot_metrics(
    config: MetricsConfig,
    *,
    input_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Path]:
    input_path = Path(input_dir) if input_dir is not None else config.output_dir
    output_path = Path(output_dir) if output_dir is not None else config.plots_dir
    output_path.mkdir(parents=True, exist_ok=True)

    selected_run_id = run_id or _select_representative_run(input_path)
    paths = {
        "tradeoff": output_path / "nimbus_tradeoff.png",
        "timeline": output_path / "nimbus_timeline.png",
        "latency_distribution": output_path / "nimbus_latency_distribution.png",
    }
    plot_tradeoff(input_path / "summary_metrics.csv", paths["tradeoff"], config.warm_slo_ms)
    plot_timeline(
        input_path / "timeline.csv",
        paths["timeline"],
        warm_slo_ms=config.warm_slo_ms,
        run_id=selected_run_id,
    )
    plot_latency_distribution(
        input_path / "latency_samples.csv",
        paths["latency_distribution"],
        warm_slo_ms=config.warm_slo_ms,
        cold_slo_ms=config.cold_slo_ms,
        run_id=selected_run_id,
    )
    return paths


def plot_tradeoff(summary_csv: Path, output_path: Path, warm_slo_ms: float) -> None:
    rows = _read_csv(summary_csv)
    by_config = _group_by_config(rows)

    fig, (ax_tradeoff, ax_cost) = plt.subplots(
        1,
        2,
        figsize=(13.5, 5.2),
        gridspec_kw={"width_ratios": [1.1, 1]},
    )

    for configuration, config_rows in by_config.items():
        x_values = _numeric_column(config_rows, "allocated_core_seconds_per_1000_successful_requests")
        y_values = _numeric_column(config_rows, "warm_p95_ms")
        if not x_values or not y_values:
            continue
        x = _median(x_values)
        y = _median(y_values)
        color = _color_for(configuration)
        x_ci = _bootstrap_ci(x_values)
        y_ci = _bootstrap_ci(y_values)
        if x_ci and y_ci and len(x_values) > 1 and len(y_values) > 1:
            ax_tradeoff.errorbar(
                x,
                y,
                xerr=[[x - x_ci[0]], [x_ci[1] - x]],
                yerr=[[y - y_ci[0]], [y_ci[1] - y]],
                fmt="o",
                markersize=8,
                color=color,
                capsize=3,
                label=configuration,
                zorder=3,
            )
        else:
            ax_tradeoff.scatter(x, y, s=95, color=color, label=configuration, zorder=3)

        slo_values = _numeric_column(config_rows, "slo_violation_pct")
        if slo_values:
            ax_tradeoff.annotate(
                f"{_median(slo_values):.1f}% SLO miss",
                (x, y),
                xytext=(7, 7),
                textcoords="offset points",
                fontsize=9,
            )

    ax_tradeoff.axhline(
        warm_slo_ms,
        color="#111827",
        linestyle="--",
        linewidth=1,
        label=f"warm SLO ({warm_slo_ms:g} ms)",
    )
    ax_tradeoff.set_title("Latency-resource trade-off")
    ax_tradeoff.set_xlabel("allocated core-seconds / 1,000 successful requests")
    ax_tradeoff.set_ylabel("warm request p95 latency (ms)")
    ax_tradeoff.grid(True, color="#d1d5db", linewidth=0.7, alpha=0.8)
    ax_tradeoff.legend(frameon=False, fontsize=9)

    labels = list(by_config)
    x_positions = list(range(len(labels)))
    width = 0.28
    allocated = [_median(_numeric_column(by_config[label], "allocated_core_seconds") or [0]) for label in labels]
    actual = [_median(_numeric_column(by_config[label], "actual_core_seconds") or [0]) for label in labels]
    peak = [_median(_numeric_column(by_config[label], "peak_allocated_cores") or [0]) for label in labels]
    ax_cost.bar([x - width for x in x_positions], allocated, width, label="allocated core-sec", color="#9ca3af")
    ax_cost.bar(x_positions, actual, width, label="actual core-sec", color="#60a5fa")
    ax_peak = ax_cost.twinx()
    ax_peak.plot([x + width for x in x_positions], peak, "o-", label="peak allocated cores", color="#dc2626")

    ax_cost.set_title("Resource cost by configuration")
    ax_cost.set_ylabel("core-seconds")
    ax_peak.set_ylabel("peak allocated cores")
    ax_cost.set_xticks(x_positions)
    ax_cost.set_xticklabels([_short_label(label) for label in labels])
    ax_cost.grid(True, axis="y", color="#d1d5db", linewidth=0.7, alpha=0.8)
    lines, line_labels = ax_cost.get_legend_handles_labels()
    peak_lines, peak_labels = ax_peak.get_legend_handles_labels()
    ax_cost.legend(lines + peak_lines, line_labels + peak_labels, frameon=False, fontsize=9)

    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def plot_timeline(
    timeline_csv: Path,
    output_path: Path,
    *,
    warm_slo_ms: float,
    run_id: str | None,
) -> None:
    rows = _filter_run(_read_csv(timeline_csv), run_id)
    by_config = _group_by_config(rows)
    reference_rows = by_config.get("Full Nimbus") or next(iter(by_config.values()), [])

    fig, axes = plt.subplots(4, 1, figsize=(13.8, 10.2), sharex=True)

    def shade_burst(ax: plt.Axes) -> None:
        burst_start: float | None = None
        for row in reference_rows + [{"minute": _last_minute(reference_rows) + 1, "nimbus_mode": ""}]:
            minute = _float(row.get("minute")) or 0.0
            is_burst = row.get("nimbus_mode") == "BURST"
            if is_burst and burst_start is None:
                burst_start = minute
            elif burst_start is not None and not is_burst:
                ax.axvspan(burst_start, minute, color="#fee2e2", alpha=0.65, zorder=0)
                burst_start = None

    for ax in axes:
        shade_burst(ax)
        ax.grid(True, color="#d1d5db", linewidth=0.7, alpha=0.75)

    minutes = [_float(row.get("minute")) or 0.0 for row in reference_rows]
    if reference_rows:
        axes[0].plot(minutes, _series(reference_rows, "offered_rps"), label="offered", color="#111827")
        axes[0].bar(
            minutes,
            _series(reference_rows, "inflight"),
            label="in-flight workload",
            color="#93c5fd",
            alpha=0.5,
        )
    for configuration, config_rows in by_config.items():
        axes[0].plot(
            _minutes(config_rows),
            _series(config_rows, "completed_rps"),
            label=f"{configuration} completed",
            color=_color_for(configuration),
            linewidth=1.8,
        )
    axes[0].set_ylabel("requests/s")
    axes[0].legend(frameon=False, ncol=3, fontsize=8.5)

    for configuration, config_rows in by_config.items():
        axes[1].plot(
            _minutes(config_rows),
            _series(config_rows, "warm_p95_ms"),
            color=_color_for(configuration),
            label=configuration,
            linewidth=1.9,
        )
    axes[1].axhline(warm_slo_ms, color="#111827", linestyle="--", linewidth=1, label="warm SLO")
    axes[1].set_ylabel("latency ms")
    axes[1].legend(frameon=False, ncol=4, fontsize=8.5)

    for configuration, config_rows in by_config.items():
        axes[2].step(
            _minutes(config_rows),
            _series(config_rows, "ready_pods"),
            where="mid",
            label=f"{configuration} ready",
            color=_color_for(configuration),
            linewidth=1.8,
        )
        axes[2].step(
            _minutes(config_rows),
            _series(config_rows, "pending_pods"),
            where="mid",
            label=f"{configuration} pending",
            color=_color_for(configuration),
            linestyle=":",
            linewidth=1.4,
            alpha=0.8,
        )
    axes[2].set_ylabel("pods")
    axes[2].legend(frameon=False, ncol=3, fontsize=8.0)

    for configuration, config_rows in by_config.items():
        axes[3].plot(
            _minutes(config_rows),
            _series(config_rows, "allocated_cpu_cores"),
            color=_color_for(configuration),
            label=f"{configuration} allocated",
            linewidth=1.9,
        )
        axes[3].plot(
            _minutes(config_rows),
            _series(config_rows, "actual_cpu_cores"),
            color=_color_for(configuration),
            label=f"{configuration} actual",
            linestyle="--",
            linewidth=1.4,
            alpha=0.78,
        )
    for row in reference_rows:
        tier = str(row.get("nimbus_tier", ""))
        if tier in {"c_min", "best_fit", "pending"}:
            marker = {"c_min": "o", "best_fit": "s", "pending": "x"}[tier]
            axes[3].scatter(
                _float(row.get("minute")) or 0.0,
                _float(row.get("allocated_cpu_cores")) or 0.0,
                marker=marker,
                color="#dc2626",
                s=36,
                zorder=3,
            )
    axes[3].set_ylabel("CPU cores")
    axes[3].set_xlabel("minute")
    axes[3].legend(frameon=False, ncol=3, fontsize=8.0)

    title_run = f" ({run_id})" if run_id else ""
    fig.suptitle(f"Nimbus timeline comparison{title_run}", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def plot_latency_distribution(
    latency_csv: Path,
    output_path: Path,
    *,
    warm_slo_ms: float,
    cold_slo_ms: float,
    run_id: str | None,
) -> None:
    rows = _filter_run(_read_csv(latency_csv), run_id)
    by_config = _group_by_config(rows)
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 5.2))

    for ax, state, slo in [(axes[0], "warm", warm_slo_ms), (axes[1], "cold", cold_slo_ms)]:
        for configuration, config_rows in by_config.items():
            values = sorted(
                value
                for value in (_float(row.get("latency_ms")) for row in config_rows if row.get("state") == state)
                if value is not None
            )
            if not values:
                continue
            y_values = [(index + 1) / len(values) for index in range(len(values))]
            ax.step(values, y_values, where="post", label=configuration, color=_color_for(configuration))

        ax.axvline(slo, color="#111827", linestyle="--", linewidth=1, label=f"{state} SLO")
        ax.set_title(f"{state.capitalize()} requests")
        ax.set_xlabel("latency (ms)")
        ax.set_ylabel("Fraction of requests <= latency")
        ax.set_ylim(0, 1.01)
        ax.grid(True, color="#d1d5db", linewidth=0.7, alpha=0.8)
        ax.legend(frameon=False, fontsize=9)

    fig.suptitle("Cold and warm latency distributions", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _group_by_config(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("configuration", "")), []).append(row)
    ordered: dict[str, list[dict[str, Any]]] = {}
    for label in CONFIG_COLORS:
        if label in grouped:
            ordered[label] = grouped.pop(label)
    for label in sorted(grouped):
        if label:
            ordered[label] = grouped[label]
    return ordered


def _filter_run(rows: list[dict[str, str]], run_id: str | None) -> list[dict[str, str]]:
    if run_id is None:
        return rows
    return [row for row in rows if row.get("run_id") == run_id]


def _select_representative_run(input_dir: Path) -> str | None:
    for filename in ["timeline.csv", "latency_samples.csv"]:
        path = input_dir / filename
        if not path.exists():
            continue
        rows = _read_csv(path)
        nimbus_runs = sorted({row.get("run_id", "") for row in rows if row.get("configuration") == "Full Nimbus"})
        if nimbus_runs and nimbus_runs[0]:
            return nimbus_runs[0]
        run_ids = sorted({row.get("run_id", "") for row in rows if row.get("run_id")})
        if run_ids:
            return run_ids[0]
    return None


def _numeric_column(rows: list[dict[str, Any]], column: str) -> list[float]:
    values = [_float(row.get(column)) for row in rows]
    return [value for value in values if value is not None and math.isfinite(value)]


def _series(rows: list[dict[str, Any]], column: str) -> list[float | None]:
    return [_float(row.get(column)) for row in rows]


def _minutes(rows: list[dict[str, Any]]) -> list[float]:
    return [_float(row.get("minute")) or 0.0 for row in rows]


def _last_minute(rows: list[dict[str, Any]]) -> float:
    minutes = _minutes(rows)
    return max(minutes) if minutes else 0.0


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _bootstrap_ci(values: list[float], *, iterations: int = 1000) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(17)
    medians = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        medians.append(_median(sample))
    medians.sort()
    return medians[int(iterations * 0.025)], medians[int(iterations * 0.975)]


def _color_for(configuration: str) -> str:
    if configuration in CONFIG_COLORS:
        return CONFIG_COLORS[configuration]
    palette = ["#059669", "#7c3aed", "#ea580c", "#0891b2"]
    return palette[abs(hash(configuration)) % len(palette)]


def _short_label(label: str) -> str:
    return {
        "Static Knative": "Static",
        "Fixed Startup Boost": "Fixed boost",
        "Full Nimbus": "Nimbus",
    }.get(label, label)
