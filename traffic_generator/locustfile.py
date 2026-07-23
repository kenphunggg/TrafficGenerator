"""Locust entrypoint for trace-driven replay."""

from __future__ import annotations

import time
import gevent
from locust import HttpUser, between, events, task
from locust.exception import StopUser

from traffic_generator.config import load_config
from traffic_generator.logging import JsonlTrafficLogger
from traffic_generator.request_builder import RequestBuilder
from traffic_generator.replay import iter_schedule
from traffic_generator.routing.direct import DirectRouter
from traffic_generator.routing.inflight_suffix import InFlightSuffixRouter
from traffic_generator.service_map import ServiceResolver
from traffic_generator.trace_loader import load_trace


_BOOTSTRAP_HOST = "http://trafficgenerator.local"


@events.init_command_line_parser.add_listener
def _add_trafficgen_options(parser) -> None:
    parser.add_argument(
        "--trafficgen-config",
        default=None,
        help="Path to trafficgen.config.toml. TRAFFICGEN_CONFIG is also supported.",
    )


class TraceReplayUser(HttpUser):
    """Single Locust user that drives the full replay schedule."""

    # HttpUser requires a host before on_start() can load trafficgen.config.toml.
    host = _BOOTSTRAP_HOST
    wait_time = between(0, 0)
    _started = False

    @task
    def replay_trace(self) -> None:
        if TraceReplayUser._started:
            raise StopUser()
        TraceReplayUser._started = True

        options = getattr(self.environment, "parsed_options", None)
        config_path = getattr(options, "trafficgen_config", None)
        config = load_config(config_path)
        if config.target.host:
            self._set_client_base_url(config.target.host)
        elif self.environment.host:
            self._set_client_base_url(self.environment.host)

        logger = JsonlTrafficLogger(
            config.logging.dir,
            log_response_body=config.logging.log_response_body,
            max_body_log_bytes=config.logging.max_body_log_bytes,
        )
        rows = load_trace(config.trace.file)  # type: ignore[arg-type]
        resolver = ServiceResolver.from_config(config)
        events_to_send = iter_schedule(rows, config, service_resolver=resolver)
        router = (
            InFlightSuffixRouter(config.routing.suffix_template)
            if config.routing.increase_service
            else DirectRouter()
        )
        builder = RequestBuilder(config)

        start = time.monotonic()
        greenlets = []
        for event in events_to_send:
            delay = event.scheduled_at_sec - (time.monotonic() - start)
            if delay > 0:
                gevent.sleep(delay)
            allocation = router.allocate(event)
            request = builder.build(event, allocation)
            logger.log_request(request)
            if config.traffic.dry_run:
                router.release(allocation, success_response=True)
                continue
            greenlets.append(
                gevent.spawn(
                    self._send_one,
                    request,
                    router,
                    logger,
                    timeout=config.routing.request_timeout_sec,
                )
            )

        if greenlets:
            gevent.joinall(greenlets)
        raise StopUser()

    def _set_client_base_url(self, host: str) -> None:
        self.host = host
        if hasattr(self.client, "base_url"):
            self.client.base_url = host.rstrip("/")

    def _send_one(self, request, router, logger, *, timeout: float) -> None:
        start = time.perf_counter()
        success_response = False
        try:
            response = self.client.request(
                request.method,
                request.url,
                data=request.body,
                headers=dict(request.headers),
                timeout=timeout,
                name=request.allocation.target_service,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            success = 200 <= response.status_code < 400
            success_response = True
            logger.log_response(
                request,
                success=success,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                response_body=response.content,
            )
        except Exception as exc:  # noqa: BLE001 - Locust/requests/gevent exceptions vary.
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.log_response(
                request,
                success=False,
                status_code=None,
                response_time_ms=elapsed_ms,
                response_body=None,
                error=str(exc),
            )
        finally:
            router.release(request.allocation, success_response=success_response)
