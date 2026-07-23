"""Routing interface."""

from __future__ import annotations

from typing import Protocol

from traffic_generator.models import AliasAllocation, RequestEvent


class Router(Protocol):
    def allocate(self, event: RequestEvent) -> AliasAllocation:
        """Choose the concrete target service for an event."""

    def release(self, allocation: AliasAllocation, *, success_response: bool) -> None:
        """Release or quarantine a target after a request completes."""
