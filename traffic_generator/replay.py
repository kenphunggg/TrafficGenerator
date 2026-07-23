"""Build deterministic replay schedules from trace rows."""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass
from itertools import groupby
from typing import Iterable, Iterator, Sequence

from .models import DryRunSummary, ReplayConfig, RequestEvent, TraceRow
from .poisson import SECONDS_PER_TRACE_MINUTE, generate_arrival_offsets, scale_count
from .service_map import ServiceResolver


@dataclass(frozen=True)
class _RawEvent:
    trace_minute: int
    arrival_offset_sec: float
    scheduled_at_sec: float
    function_id: str
    service_base: str
    row_event_index: int


def selected_rows(rows: Iterable[TraceRow], config: ReplayConfig) -> list[TraceRow]:
    return sorted(
        (
            row
            for row in rows
            if (config.trace.start_minute is None or row.minute >= config.trace.start_minute)
            and (config.trace.end_minute is None or row.minute <= config.trace.end_minute)
        ),
        key=lambda row: (row.minute, row.function_id),
    )


def iter_schedule(
    rows: Iterable[TraceRow],
    config: ReplayConfig,
    *,
    service_resolver: ServiceResolver | None = None,
    rng: random.Random | None = None,
) -> Iterator[RequestEvent]:
    selected = selected_rows(rows, config)
    if not selected:
        return

    first_minute = min(row.minute for row in selected)
    resolver = service_resolver or ServiceResolver.from_config(config)
    generator = rng or random.Random(config.traffic.random_seed)
    request_id = 1

    for minute, minute_rows_iter in groupby(selected, key=lambda row: row.minute):
        raw_events: list[_RawEvent] = []
        for row in minute_rows_iter:
            request_count = scale_count(row.count, config.traffic.scale)
            service_base = resolver.resolve(row.function_id)
            offsets = generate_arrival_offsets(request_count, rng=generator)
            for row_event_index, offset in enumerate(offsets):
                raw_events.append(
                    _RawEvent(
                        trace_minute=row.minute,
                        arrival_offset_sec=offset,
                        scheduled_at_sec=((row.minute - first_minute) * SECONDS_PER_TRACE_MINUTE)
                        + offset,
                        function_id=row.function_id,
                        service_base=service_base,
                        row_event_index=row_event_index,
                    )
                )

        raw_events.sort(
            key=lambda event: (
                event.arrival_offset_sec,
                event.function_id,
                event.row_event_index,
            )
        )
        for event in raw_events:
            yield RequestEvent(
                request_id=request_id,
                trace_minute=event.trace_minute,
                arrival_offset_sec=event.arrival_offset_sec,
                scheduled_at_sec=event.scheduled_at_sec,
                function_id=event.function_id,
                service_base=event.service_base,
                request_index=request_id,
            )
            request_id += 1


def build_schedule(
    rows: Sequence[TraceRow],
    config: ReplayConfig,
    *,
    service_resolver: ServiceResolver | None = None,
    rng: random.Random | None = None,
) -> list[RequestEvent]:
    return list(iter_schedule(rows, config, service_resolver=service_resolver, rng=rng))


def original_request_total(rows: Iterable[TraceRow]) -> int:
    return sum(row.count for row in rows)


def scheduled_request_total(rows: Iterable[TraceRow], config: ReplayConfig) -> int:
    return sum(scale_count(row.count, config.traffic.scale) for row in selected_rows(rows, config))


def scheduled_request_total_by_service(
    rows: Iterable[TraceRow],
    config: ReplayConfig,
    *,
    service_resolver: ServiceResolver | None = None,
) -> dict[str, int]:
    resolver = service_resolver or ServiceResolver.from_config(config)
    totals: dict[str, int] = {}
    for row in selected_rows(rows, config):
        service_base = resolver.resolve(row.function_id)
        totals[service_base] = totals.get(service_base, 0) + scale_count(
            row.count,
            config.traffic.scale,
        )
    return totals


def selected_minute_count(rows: Sequence[TraceRow]) -> int:
    return len({row.minute for row in rows})


def estimate_max_aliases(
    events: Iterable[RequestEvent],
    *,
    assumed_service_time_sec: float | None,
) -> dict[str, int]:
    if assumed_service_time_sec is None:
        counts: dict[str, int] = {}
        for event in events:
            counts[event.service_base] = counts.get(event.service_base, 0) + 1
        return counts

    max_by_service: dict[str, int] = {}
    active_by_service: dict[str, list[float]] = {}
    for event in events:
        active = active_by_service.setdefault(event.service_base, [])
        while active and active[0] <= event.scheduled_at_sec:
            heapq.heappop(active)
        heapq.heappush(active, event.scheduled_at_sec + assumed_service_time_sec)
        max_by_service[event.service_base] = max(
            max_by_service.get(event.service_base, 0),
            len(active),
        )
    return max_by_service


def build_dry_run_summary(
    rows: Sequence[TraceRow],
    events: Iterable[RequestEvent],
    config: ReplayConfig,
    *,
    service_resolver: ServiceResolver | None = None,
    max_aliases: dict[str, int] | None = None,
) -> DryRunSummary:
    selected = selected_rows(rows, config)
    scheduled_total = scheduled_request_total(selected, config)
    resolver = service_resolver or ServiceResolver.from_config(config)
    if max_aliases is None and config.routing.increase_service:
        if config.routing.dry_run_assumed_service_time_sec is None:
            max_aliases = scheduled_request_total_by_service(
                selected,
                config,
                service_resolver=resolver,
            )
        else:
            max_aliases = estimate_max_aliases(
                events,
                assumed_service_time_sec=config.routing.dry_run_assumed_service_time_sec,
            )

    return DryRunSummary(
        trace_file=config.trace.file,  # type: ignore[arg-type]
        scale=config.traffic.scale,
        minutes=selected_minute_count(selected),
        original_requests=original_request_total(selected),
        scheduled_requests=scheduled_total,
        method=config.request.method,
        namespace=config.target.namespace,
        path=config.target.path,
        increase_service=config.routing.increase_service,
        max_allocated_alias_by_service=max_aliases or {},
        log_dir=config.logging.dir,
    )
