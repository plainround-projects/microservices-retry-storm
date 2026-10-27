"""Exponential backoff with full jitter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Backoff:
    """The pause after failed attempt ``n`` is a random number between 0 and ``min(cap, base * multiplier^(n-1))``.

    Full jitter spreads clients out in time; without it, every client that failed together retries together.
    """

    base: float = 0.05
    cap: float = 0.5
    multiplier: float = 2.0

    def delay(self, attempt: int, rand: float) -> float:
        """Pause after attempt number ``attempt`` (1-based) for a random draw ``rand`` in [0, 1)."""
        return min(self.cap, self.base * self.multiplier ** (attempt - 1)) * rand
