"""Direct routing without dynamic service suffixes."""

from __future__ import annotations

from traffic_generator.models import AliasAllocation, RequestEvent


class DirectRouter:
    def allocate(self, event: RequestEvent) -> AliasAllocation:
        return AliasAllocation(
            service_base=event.service_base,
            target_service=event.service_base,
            alias_index=None,
            decision="direct",
        )

    def release(self, allocation: AliasAllocation, *, success_response: bool) -> None:
        return None
