"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol

from resilience.deadline import Deadline
from search.domain.models import SearchResult


class PricingUpstream(Protocol):
    """One attempt at asking pricing. Raises ``AttemptFailed``."""

    async def quote(
        self, query: str, sku: str, timeout: float, deadline: Deadline | None, attempt: int
    ) -> SearchResult:
        """The priced result for the sku."""


class Telemetry(Protocol):
    """Counters the experiments read."""

    def shed(self) -> None:
        """A request was refused because the concurrency limit was full."""

    def degraded(self) -> None:
        """A partial result was returned because pricing could not be asked in time."""
