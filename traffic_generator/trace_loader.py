"""CSV trace loading."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .models import TraceRow

_NAME_COLUMNS = ("function_id", "app_name")
_COUNT_COLUMNS = ("count", "requests_per_minute")


class TraceFormatError(ValueError):
    """Raised when a trace file does not match the expected shape."""


def load_trace(path: str | Path) -> list[TraceRow]:
    trace_path = Path(path)
    if not trace_path.exists():
        raise FileNotFoundError(f"trace file not found: {trace_path}")

    with trace_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise TraceFormatError(f"empty trace file: {trace_path}")
        minute_col, name_col, count_col = _select_columns(reader.fieldnames)

        grouped: defaultdict[tuple[int, str], int] = defaultdict(int)
        row_count = 0
        for row_no, row in enumerate(reader, start=2):
            row_count += 1
            minute = _parse_int(row.get(minute_col), row_no, minute_col)
            function_id = (row.get(name_col) or "").strip()
            if not function_id:
                raise TraceFormatError(f"row {row_no}: {name_col} is empty")
            count = _parse_int(row.get(count_col), row_no, count_col)
            if count < 0:
                raise TraceFormatError(f"row {row_no}: {count_col} must be >= 0")
            grouped[(minute, function_id)] += count

    if row_count == 0:
        raise TraceFormatError(f"empty trace file: {trace_path}")

    return [
        TraceRow(minute=minute, function_id=function_id, count=count)
        for (minute, function_id), count in sorted(grouped.items())
    ]


def filter_trace_rows(
    rows: Iterable[TraceRow],
    *,
    start_minute: int | None = None,
    end_minute: int | None = None,
) -> list[TraceRow]:
    selected: list[TraceRow] = []
    for row in rows:
        if start_minute is not None and row.minute < start_minute:
            continue
        if end_minute is not None and row.minute > end_minute:
            continue
        selected.append(row)
    return selected


def _select_columns(fieldnames: list[str]) -> tuple[str, str, str]:
    fields = {field.strip(): field for field in fieldnames if field is not None}
    if "minute" not in fields:
        raise TraceFormatError("trace is missing required column: minute")

    name_col = next((fields[name] for name in _NAME_COLUMNS if name in fields), None)
    count_col = next((fields[name] for name in _COUNT_COLUMNS if name in fields), None)
    if name_col is None:
        raise TraceFormatError(
            "trace is missing workload column: expected function_id or app_name"
        )
    if count_col is None:
        raise TraceFormatError(
            "trace is missing count column: expected count or requests_per_minute"
        )
    return fields["minute"], name_col, count_col


def _parse_int(value: str | None, row_no: int, column: str) -> int:
    if value is None or value.strip() == "":
        raise TraceFormatError(f"row {row_no}: {column} is empty")
    try:
        return int(value)
    except ValueError as exc:
        raise TraceFormatError(f"row {row_no}: {column} must be an integer") from exc
