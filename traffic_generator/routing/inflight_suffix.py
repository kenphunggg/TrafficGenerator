"""In-flight-aware service suffix routing."""

from __future__ import annotations

import heapq
from collections import defaultdict
from time import sleep as blocking_sleep

from traffic_generator.models import AliasAllocation, AliasState, RequestEvent


class InFlightSuffixRouter:
    def __init__(
        self,
        suffix_template: str = "{service_base}-{index:03d}",
        *,
        max_aliases: int | None = None,
        wait_poll_sec: float = 0.05,
    ) -> None:
        if max_aliases is not None and max_aliases < 1:
            raise ValueError("max_aliases must be >= 1")
        self.suffix_template = suffix_template
        self.max_aliases = max_aliases
        self.wait_poll_sec = wait_poll_sec
        self._states: defaultdict[str, AliasState] = defaultdict(AliasState)
        self._max_suffix_by_service: dict[str, int] = {}

    def allocate(self, event: RequestEvent) -> AliasAllocation:
        waited = False
        while True:
            allocation = self._try_allocate(event, waited=waited)
            if allocation is not None:
                return allocation
            waited = True
            _sleep(self.wait_poll_sec)

    def _try_allocate(self, event: RequestEvent, *, waited: bool) -> AliasAllocation | None:
        state = self._states[event.service_base]
        if state.free_aliases:
            alias_index = heapq.heappop(state.free_aliases)
            decision = "wait_reuse_warm" if waited else "reuse_warm"
        elif self.max_aliases is None or state.next_suffix <= self.max_aliases:
            alias_index = state.next_suffix
            state.next_suffix += 1
            decision = "new"
        elif state.busy_aliases:
            return None
        else:
            raise RuntimeError(
                f"no usable aliases remain for {event.service_base}; "
                f"all {self.max_aliases} configured aliases are unavailable"
            )

        state.busy_aliases.add(alias_index)
        self._max_suffix_by_service[event.service_base] = max(
            self._max_suffix_by_service.get(event.service_base, 0), alias_index
        )
        return AliasAllocation(
            service_base=event.service_base,
            target_service=self.suffix_template.format(
                service_base=event.service_base,
                index=alias_index,
            ),
            alias_index=alias_index,
            decision=decision,
        )

    def release(self, allocation: AliasAllocation, *, success_response: bool) -> None:
        if allocation.alias_index is None:
            return
        state = self._states[allocation.service_base]
        alias_index = allocation.alias_index
        state.busy_aliases.discard(alias_index)
        if success_response:
            if alias_index not in state.quarantined_aliases:
                heapq.heappush(state.free_aliases, alias_index)
        else:
            state.quarantined_aliases.add(alias_index)

    @property
    def max_suffix_by_service(self) -> dict[str, int]:
        return dict(self._max_suffix_by_service)

    def state_for(self, service_base: str) -> AliasState:
        return self._states[service_base]


def _sleep(seconds: float) -> None:
    try:
        import gevent
    except ModuleNotFoundError:
        blocking_sleep(seconds)
    else:
        gevent.sleep(seconds)
