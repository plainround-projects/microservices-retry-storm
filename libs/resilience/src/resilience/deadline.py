"""A deadline travels with a request: each hop gets what is left, never more."""

from __future__ import annotations

from dataclasses import dataclass

DEADLINE_HEADER = "X-Deadline-Ms"


@dataclass(frozen=True, slots=True)
class Deadline:
    """The moment (on a monotonic clock, in seconds) by which the whole request must be answered."""

    expires_at: float

    @classmethod
    def after(cls, now: float, seconds: float) -> Deadline:
        """A deadline ``seconds`` from ``now``."""
        return cls(now + seconds)

    @classmethod
    def from_header(cls, value: str | None, now: float, ceiling_seconds: float = 60.0) -> Deadline | None:
        """Parse the remaining milliseconds a caller sent; ignore a missing, malformed or negative value.

        A huge value is clamped to ``ceiling_seconds`` so a caller cannot make this service wait forever.
        """
        if value is None:
            return None
        try:
            remaining_ms = float(value)
        except ValueError:
            return None
        if remaining_ms != remaining_ms or remaining_ms < 0:
            return None
        return cls.after(now, min(remaining_ms / 1000.0, ceiling_seconds))

    def remaining(self, now: float) -> float:
        """Seconds left, never negative."""
        return max(0.0, self.expires_at - now)

    def expired(self, now: float) -> bool:
        """True when nothing is left."""
        return self.expires_at <= now

    def header_value(self, now: float) -> str:
        """The remaining milliseconds, as sent to the next hop."""
        return str(int(self.remaining(now) * 1000))

    def attempt_timeout(self, now: float, hop_limit: float, margin: float) -> float:
        """The timeout for one call: the hop's own limit, or what is left minus ``margin``, whichever is smaller."""
        return max(0.0, min(hop_limit, self.remaining(now) - margin))
