"""The three resilience profiles the experiments switch between, at runtime, in every service."""

from __future__ import annotations

from enum import StrEnum


class Profile(StrEnum):
    """What each service does about failure.

    - ``NAIVE``: every client retries three times, immediately, with a 2 second timeout. The lesson's starting point.
    - ``DEADLINE_RETRY``: the gateway sets a 2 second deadline and is the only layer that retries, with backoff,
      jitter and a 10 percent retry budget; every other hop makes one attempt inside what is left of the deadline and
      answers with a degraded result instead of an error.
    - ``FULL``: as above, plus a circuit breaker on pricing's call to the stock store and load shedding at search.
    """

    NAIVE = "naive"
    DEADLINE_RETRY = "deadline-retry"
    FULL = "full"


class ProfileHolder:
    """The profile a service is running with. Replaced as a whole, so a reader never sees a half-changed state."""

    def __init__(self, initial: Profile = Profile.NAIVE) -> None:
        self._profile = initial

    def get(self) -> Profile:
        """The current profile."""
        return self._profile

    def set(self, profile: Profile) -> None:
        """Switch profile."""
        self._profile = profile
