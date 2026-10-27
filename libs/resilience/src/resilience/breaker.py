"""A circuit breaker: stop calling a dependency that is failing, and probe it now and then."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from enum import IntEnum


class BreakerState(IntEnum):
    """The numeric value is what the metrics gauge exports."""

    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


class CircuitBreaker:
    """Opens when ``failure_ratio`` of the calls of the last ``window_seconds`` failed (and there were ``min_calls``).

    While open it refuses every call locally, without touching the network, for ``open_seconds``. Then it lets
    ``half_open_probes`` calls through: if they all succeed it closes, and one failure opens it again.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        failure_ratio: float = 0.5,
        min_calls: int = 10,
        window_seconds: float = 10.0,
        open_seconds: float = 5.0,
        half_open_probes: int = 3,
    ) -> None:
        self._clock = clock
        self._failure_ratio = failure_ratio
        self._min_calls = min_calls
        self._window = window_seconds
        self._open_seconds = open_seconds
        self._probes = half_open_probes
        self._calls: deque[tuple[float, bool]] = deque()
        self._state = BreakerState.CLOSED
        self._opened_at = 0.0
        self._probes_started = 0
        self._probes_succeeded = 0

    @property
    def state(self) -> BreakerState:
        """The current state (an open breaker becomes half-open once its cool-down has passed)."""
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self._open_seconds:
            self._state = BreakerState.HALF_OPEN
            self._probes_started = 0
            self._probes_succeeded = 0
        return self._state

    def allow(self) -> bool:
        """May a call go out now? In half-open state only a limited number of probes may."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        if self._probes_started < self._probes:
            self._probes_started += 1
            return True
        return False

    def record_success(self) -> None:
        """A call succeeded."""
        if self.state is BreakerState.HALF_OPEN:
            self._probes_succeeded += 1
            if self._probes_succeeded >= self._probes:
                self._state = BreakerState.CLOSED
                self._calls.clear()
            return
        self._add(True)

    def record_failure(self) -> None:
        """A call failed or timed out."""
        if self.state is BreakerState.HALF_OPEN:
            self._open()
            return
        self._add(False)
        failures = sum(1 for _, ok in self._calls if not ok)
        if len(self._calls) >= self._min_calls and failures / len(self._calls) >= self._failure_ratio:
            self._open()

    def _add(self, ok: bool) -> None:
        now = self._clock()
        self._calls.append((now, ok))
        while self._calls and self._calls[0][0] <= now - self._window:
            self._calls.popleft()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._calls.clear()
