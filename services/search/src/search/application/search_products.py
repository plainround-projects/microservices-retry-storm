"""Search: resolve the query, then ask pricing."""

from __future__ import annotations

from collections.abc import Callable

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded, Overloaded
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import Profile
from resilience.retry import AttemptFailed, RetriesExhausted, run_naive
from search.application.ports import PricingUpstream, Telemetry
from search.domain.models import SearchResult, resolve_sku

HOP_TIMEOUT = 1.2
MARGIN = 0.05
MIN_USEFUL = 0.1


class PricingUnavailable(Exception):
    """The naive chain's failure: pricing could not be reached after every attempt."""


class SearchProducts:
    """What search does about pricing depends on the profile.

    - ``naive``: three immediate attempts with a 2 second timeout each; if all fail, the error goes up.
    - ``deadline-retry``: one attempt inside what is left of the caller's deadline; on failure, a partial result.
    - ``full``: the same, plus load shedding: beyond ``limiter.limit`` requests in progress, a request is refused at
      once with ``Overloaded`` (a 503 with ``Retry-After``) instead of joining a queue it cannot leave in time.
    """

    def __init__(
        self,
        pricing: PricingUpstream,
        limiter: ConcurrencyLimiter,
        profile: Callable[[], Profile],
        clock: Callable[[], float],
        telemetry: Telemetry,
    ) -> None:
        self._pricing = pricing
        self._limiter = limiter
        self._profile = profile
        self._clock = clock
        self._telemetry = telemetry

    async def __call__(self, query: str, deadline: Deadline | None) -> SearchResult:
        """Return the priced result; raise ``Overloaded``, ``DeadlineExceeded`` or ``PricingUnavailable``."""
        profile = self._profile()
        sku = resolve_sku(query)
        shedding = profile is Profile.FULL
        if shedding and not self._limiter.try_acquire():
            self._telemetry.shed()
            raise Overloaded(retry_after=1.0)
        try:
            if profile is Profile.NAIVE:
                return await self._naive(query, sku)
            return await self._once(query, sku, deadline)
        finally:
            if shedding:
                self._limiter.release()

    async def _naive(self, query: str, sku: str) -> SearchResult:
        counter = {"n": 0}

        async def attempt(timeout: float) -> SearchResult:
            counter["n"] += 1
            return await self._pricing.quote(query, sku, timeout, None, counter["n"])

        try:
            return await run_naive(attempt)
        except RetriesExhausted as error:
            raise PricingUnavailable(str(error)) from error

    async def _once(self, query: str, sku: str, deadline: Deadline | None) -> SearchResult:
        timeout = HOP_TIMEOUT if deadline is None else deadline.attempt_timeout(self._clock(), HOP_TIMEOUT, MARGIN)
        if timeout < MIN_USEFUL:
            raise DeadlineExceeded
        try:
            return await self._pricing.quote(query, sku, timeout, deadline, 1)
        except AttemptFailed:
            self._telemetry.degraded()
            return SearchResult(query, sku, None, None, degraded=True, stale=False)
