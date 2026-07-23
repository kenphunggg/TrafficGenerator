"""In-flight-aware service suffix routing."""

from __future__ import annotations

import heapq
from collections import defaultdict

from traffic_generator.models import AliasAllocation, AliasState, RequestEvent


class InFlightSuffixRouter:
    def __init__(self, suffix_template: str = "{service_base}-{index:03d}") -> None:
        self.suffix_template = suffix_template
        self._states: defaultdict[str, AliasState] = defaultdict(AliasState)
        self._max_suffix_by_service: dict[str, int] = {}

    def allocate(self, event: RequestEvent) -> AliasAllocation:
        state = self._states[event.service_base]
        if state.free_aliases:
            alias_index = heapq.heappop(state.free_aliases)
            decision = "reuse_warm"
        else:
            alias_index = state.next_suffix
            state.next_suffix += 1
            decision = "new"

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
