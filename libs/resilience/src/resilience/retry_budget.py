"""A retry budget: retries may add at most a fixed fraction of load, however many requests fail."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable


class RetryBudget:
    """Allows a retry only while the retries of the last ``window_seconds`` stay under ``ratio`` times the requests.

    ``min_retries`` lets a quiet service retry a little (otherwise 10 percent of 5 requests is zero retries).
    When many requests fail at once, the budget runs out and the rest fail fast instead of piling retries on a
    struggling dependency, which is the point.
    """

    def __init__(
        self, clock: Callable[[], float], ratio: float = 0.1, window_seconds: float = 10.0, min_retries: int = 3
    ) -> None:
        self._clock = clock
        self._ratio = ratio
        self._window = window_seconds
        self._min_retries = min_retries
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()

    def _trim(self, now: float) -> None:
        for events in (self._requests, self._retries):
            while events and events[0] <= now - self._window:
                events.popleft()

    def record_request(self) -> None:
        """Count one first attempt (not a retry)."""
        now = self._clock()
        self._trim(now)
        self._requests.append(now)

    def try_acquire_retry(self) -> bool:
        """Spend one retry if the budget allows; return whether it was granted."""
        now = self._clock()
        self._trim(now)
        allowed = max(self._min_retries, int(self._ratio * len(self._requests)))
        if len(self._retries) >= allowed:
            return False
        self._retries.append(now)
        return True

    @property
    def retries_in_window(self) -> int:
        """Retries spent in the current window."""
        self._trim(self._clock())
        return len(self._retries)

    @property
    def requests_in_window(self) -> int:
        """First attempts in the current window."""
        self._trim(self._clock())
        return len(self._requests)
