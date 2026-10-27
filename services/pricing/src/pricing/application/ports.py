"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol

from pricing.domain.models import Quote
from resilience.breaker import BreakerState
from resilience.deadline import Deadline


class StockUpstream(Protocol):
    """One attempt at asking the stock store. Raises ``AttemptFailed``."""

    async def check(self, sku: str, timeout: float, deadline: Deadline | None, attempt: int) -> int:
        """Units available for the sku."""


class QuoteCache(Protocol):
    """The last good quote per sku, the fallback while the stock store is unreachable."""

    def get(self, sku: str) -> Quote | None:
        """The last known quote, or ``None``."""

    def put(self, quote: Quote) -> None:
        """Remember a fresh quote."""


class Telemetry(Protocol):
    """Counters the experiments read."""

    def fallback(self, reason: str) -> None:
        """A degraded answer was returned."""

    def breaker_rejected(self) -> None:
        """The breaker refused to call the stock store."""

    def breaker_state(self, state: BreakerState) -> None:
        """The breaker's state, after it changed."""
