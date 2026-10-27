"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol

from gateway.domain.models import SearchAnswer
from resilience.deadline import Deadline


class SearchUpstream(Protocol):
    """One attempt at asking search. Raises ``AttemptFailed``."""

    async def search(self, query: str, timeout: float, deadline: Deadline | None, attempt: int) -> SearchAnswer:
        """The answer to the query."""


class Telemetry(Protocol):
    """Counters the experiments read."""

    def retry(self, reason: str) -> None:
        """A retry was issued."""

    def outcome(self, outcome: str) -> None:
        """The request ended as ``ok``, ``degraded`` or ``unavailable``."""
