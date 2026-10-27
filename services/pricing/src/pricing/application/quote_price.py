"""Quote a price: ask the stock store, protected by the profile's policy."""

from __future__ import annotations

from collections.abc import Callable

from pricing.application.ports import QuoteCache, StockUpstream, Telemetry
from pricing.domain.models import Quote, fallback_quote, quote_from_stock
from resilience.breaker import CircuitBreaker
from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from resilience.profile import Profile
from resilience.retry import AttemptFailed, RetriesExhausted, run_naive

HOP_TIMEOUT = 0.7
MARGIN = 0.05
MIN_USEFUL = 0.1


class StockUnavailable(Exception):
    """The naive chain's failure: the stock store could not be reached after every attempt."""


class QuotePrice:
    """What pricing does about the stock store depends on the profile.

    - ``naive``: three immediate attempts with a 2 second timeout each; if all fail, the error goes up.
    - ``deadline-retry``: one attempt inside what is left of the caller's deadline; on failure, a degraded answer.
    - ``full``: the same, behind a circuit breaker. While the breaker is open the stock store is not called at all
      and the degraded answer is returned at once.
    """

    def __init__(
        self,
        stock: StockUpstream,
        cache: QuoteCache,
        breaker: CircuitBreaker,
        profile: Callable[[], Profile],
        clock: Callable[[], float],
        telemetry: Telemetry,
    ) -> None:
        self._stock = stock
        self._cache = cache
        self._breaker = breaker
        self._profile = profile
        self._clock = clock
        self._telemetry = telemetry

    async def __call__(self, sku: str, deadline: Deadline | None) -> Quote:
        """Return a quote; raise :class:`StockUnavailable` (naive) or :class:`DeadlineExceeded`."""
        profile = self._profile()
        if profile is Profile.NAIVE:
            return await self._naive(sku)
        if profile is Profile.FULL and not self._breaker.allow():
            self._telemetry.breaker_rejected()
            return self._degrade(sku, "breaker open")
        timeout = HOP_TIMEOUT if deadline is None else deadline.attempt_timeout(self._clock(), HOP_TIMEOUT, MARGIN)
        if timeout < MIN_USEFUL:
            raise DeadlineExceeded
        try:
            quantity = await self._stock.check(sku, timeout, deadline, 1)
        except AttemptFailed as error:
            if profile is Profile.FULL:
                self._breaker.record_failure()
                self._telemetry.breaker_state(self._breaker.state)
            return self._degrade(sku, error.reason)
        if profile is Profile.FULL:
            self._breaker.record_success()
            self._telemetry.breaker_state(self._breaker.state)
        return self._fresh(sku, quantity)

    async def _naive(self, sku: str) -> Quote:
        counter = {"n": 0}

        async def attempt(timeout: float) -> int:
            counter["n"] += 1
            return await self._stock.check(sku, timeout, None, counter["n"])

        try:
            quantity = await run_naive(attempt)
        except RetriesExhausted as error:
            raise StockUnavailable(str(error)) from error
        return self._fresh(sku, quantity)

    def _fresh(self, sku: str, quantity: int) -> Quote:
        quote = quote_from_stock(sku, quantity)
        self._cache.put(quote)
        return quote

    def _degrade(self, sku: str, reason: str) -> Quote:
        self._telemetry.fallback(reason)
        return fallback_quote(sku, self._cache.get(sku))
