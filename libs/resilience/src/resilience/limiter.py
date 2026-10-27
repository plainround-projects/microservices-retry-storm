"""Load shedding: refuse work beyond what the service can do, immediately, instead of queueing it."""

from __future__ import annotations


class ConcurrencyLimiter:
    """At most ``limit`` requests in progress. ``try_acquire`` never waits: a request over the limit is shed."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._in_flight = 0

    @property
    def limit(self) -> int:
        """The maximum number of requests in progress."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """Requests currently holding a slot."""
        return self._in_flight

    def try_acquire(self) -> bool:
        """Take a slot if one is free."""
        if self._in_flight >= self._limit:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        """Give a slot back."""
        if self._in_flight > 0:
            self._in_flight -= 1
