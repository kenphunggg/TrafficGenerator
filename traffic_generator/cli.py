"""Command-line helpers for validation and dry-run schedule inspection."""

from __future__ import annotations

import argparse
import heapq
import sys
from dataclasses import replace
from typing import Iterable, Sequence

from .config import load_config
from .models import AliasAllocation, ReplayConfig, RequestEvent
from .request_builder import RequestBuilder
from .replay import build_dry_run_summary, iter_schedule, scheduled_request_total
from .routing.direct import DirectRouter
from .routing.inflight_suffix import InFlightSuffixRouter
from .service_map import ServiceResolver
from .trace_loader import load_trace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trace-driven TrafficGenerator utilities")
    parser.add_argument("--config", help="Path to trafficgen.config.toml")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate config and trace, then exit without printing a schedule",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned requests without sending HTTP traffic",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum dry-run request lines to print; use 0 for no request lines",
    )
    parser.add_argument(
        "--assumed-service-time-sec",
        type=float,
        help="Dry-run alias reuse estimate; unset means conservative no-reuse estimate",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args[:1] == ["metrics"]:
        from .metrics_cli import main as metrics_main

        return metrics_main(raw_args[1:])
    if raw_args[:1] == ["experiment"]:
        from .experiment_cli import main as experiment_main

        return experiment_main(raw_args[1:])

    args = build_parser().parse_args(raw_args)
    config = load_config(args.config)
    if args.dry_run:
        config = replace(config, traffic=replace(config.traffic, dry_run=True))
    if args.assumed_service_time_sec is not None:
        config = replace(
            config,
            routing=replace(
                config.routing,
                dry_run_assumed_service_time_sec=args.assumed_service_time_sec,
            ),
        )

    rows = load_trace(config.trace.file)  # type: ignore[arg-type]
    resolver = ServiceResolver.from_config(config)

    if args.validate and not config.traffic.dry_run and not args.dry_run:
        _print_warnings(config)
        print(f"valid config: {config.config_path}")
        print(f"trace rows: {len(rows)}")
        print(f"scheduled requests: {scheduled_request_total(rows, config)}")
        return 0

    _print_warnings(config)
    _print_dry_run(config, iter_schedule(rows, config, service_resolver=resolver), rows, limit=args.limit)
    return 0


def _print_warnings(config: ReplayConfig) -> None:
    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _print_dry_run(
    config: ReplayConfig,
    events: Iterable[RequestEvent],
    rows,
    *,
    limit: int,
) -> None:
    router = (
        InFlightSuffixRouter(config.routing.suffix_template)
        if config.routing.increase_service
        else DirectRouter()
    )
    builder = RequestBuilder(config)
    completion_heap: list[tuple[float, int, AliasAllocation]] = []
    estimate_from_events = config.routing.dry_run_assumed_service_time_sec is not None
    max_aliases: dict[str, int] | None = {} if estimate_from_events else None
    printed = 0

    if limit == 0 and not estimate_from_events:
        events = []

    for event in events:
        if limit > 0 and printed >= limit and not estimate_from_events:
            break
        _release_dry_run_completions(config, router, completion_heap, event.scheduled_at_sec)
        allocation = router.allocate(event)
        if max_aliases is not None and allocation.alias_index is not None:
            max_aliases[allocation.service_base] = max(
                max_aliases.get(allocation.service_base, 0),
                allocation.alias_index,
            )
        _schedule_dry_run_completion(config, completion_heap, event, allocation)

        should_print = limit < 0 or (limit > 0 and printed < limit)
        if should_print:
            request = builder.build(event, allocation)
            print(
                "request_id={request_id} minute={minute} offset={offset:.3f} "
                "method={method} function_id={function_id} service_base={service_base} "
                "target={target} url={url}".format(
                    request_id=event.request_id,
                    minute=event.trace_minute,
                    offset=event.arrival_offset_sec,
                    method=request.method,
                    function_id=event.function_id,
                    service_base=event.service_base,
                    target=allocation.target_service,
                    url=request.url,
                )
            )
            printed += 1

    if not config.routing.increase_service:
        max_aliases = {}

    summary = build_dry_run_summary(
        rows,
        [],
        config,
        service_resolver=ServiceResolver.from_config(config),
        max_aliases=max_aliases,
    )
    print("trace_file={}".format(summary.trace_file))
    print("scale={}".format(summary.scale))
    print("minutes={}".format(summary.minutes))
    print("original_requests={}".format(summary.original_requests))
    print("scheduled_requests={}".format(summary.scheduled_requests))
    print("method={}".format(summary.method))
    print("namespace={}".format(summary.namespace))
    print("path={}".format(summary.path))
    print("increase_service={}".format(str(summary.increase_service).lower()))
    for service_base, max_suffix in summary.max_allocated_alias_by_service.items():
        if max_suffix:
            print(f"required_ksvc_aliases={service_base}-001..{service_base}-{max_suffix:03d}")
    print("log_dir={}".format(summary.log_dir))


def _release_dry_run_completions(
    config: ReplayConfig,
    router,
    completion_heap: list[tuple[float, int, AliasAllocation]],
    scheduled_at_sec: float,
) -> None:
    if config.routing.dry_run_assumed_service_time_sec is None:
        return
    while completion_heap and completion_heap[0][0] <= scheduled_at_sec:
        _, _, allocation = heapq.heappop(completion_heap)
        router.release(allocation, success_response=True)


def _schedule_dry_run_completion(
    config: ReplayConfig,
    completion_heap: list[tuple[float, int, AliasAllocation]],
    event: RequestEvent,
    allocation: AliasAllocation,
) -> None:
    service_time = config.routing.dry_run_assumed_service_time_sec
    if service_time is None:
        return
    heapq.heappush(
        completion_heap,
        (event.scheduled_at_sec + service_time, event.request_id, allocation),
    )


if __name__ == "__main__":
    raise SystemExit(main())
