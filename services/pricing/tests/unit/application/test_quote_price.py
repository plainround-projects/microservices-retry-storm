from decimal import Decimal

import pytest

from pricing.application.quote_price import QuotePrice, StockUnavailable
from pricing.domain.models import Quote, base_price
from pricing.infrastructure.cache import InMemoryQuoteCache
from resilience.breaker import BreakerState, CircuitBreaker
from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from resilience.profile import Profile
from resilience.retry import AttemptFailed


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Stock:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def check(self, sku, timeout, deadline, attempt):
        self.calls.append((timeout, deadline is not None, attempt))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class Telemetry:
    def __init__(self):
        self.fallbacks = []
        self.rejected = 0
        self.states = []

    def fallback(self, reason):
        self.fallbacks.append(reason)

    def breaker_rejected(self):
        self.rejected += 1

    def breaker_state(self, state):
        self.states.append(state)


def make(stock, profile=Profile.FULL):
    clock = Clock()
    telemetry = Telemetry()
    cache = InMemoryQuoteCache(clock)
    breaker = CircuitBreaker(
        clock, failure_ratio=0.5, min_calls=4, window_seconds=10, open_seconds=5, half_open_probes=2
    )
    return QuotePrice(stock, cache, breaker, lambda: profile, clock, telemetry), clock, cache, breaker, telemetry


async def test_a_stock_reading_becomes_a_fresh_quote_and_is_cached():
    use_case, clock, cache, _, _ = make(Stock(5))
    quote = await use_case("sku-001", Deadline.after(clock(), 2.0))
    assert (quote.in_stock, quote.degraded, quote.stale) == (True, False, False)
    assert cache.get("sku-001") == quote


async def test_low_stock_raises_the_price_by_fifteen_percent():
    use_case, _, _, _, _ = make(Stock(3))
    quote = await use_case("sku-001", None)
    assert quote.price == (base_price("sku-001") * Decimal("1.15")).quantize(Decimal("0.01"))


async def test_one_attempt_gets_the_hop_limit_or_what_is_left_of_the_deadline():
    stock = Stock(5)
    use_case, clock, _, _, _ = make(stock)
    await use_case("sku-001", Deadline.after(clock(), 2.0))
    clock.now += 1.5
    await use_case("sku-001", Deadline.after(clock() - 1.5, 2.0))
    assert [round(t, 2) for t, _, _ in stock.calls] == [0.7, 0.45]
    assert all(sent for _, sent, _ in stock.calls)


async def test_a_deadline_too_short_to_try_is_refused_without_calling_stock():
    stock = Stock(5)
    use_case, clock, _, _, _ = make(stock)
    with pytest.raises(DeadlineExceeded):
        await use_case("sku-001", Deadline.after(clock(), 0.1))
    assert stock.calls == []


async def test_a_failure_returns_the_last_known_quote_flagged_stale():
    stock = Stock(5, AttemptFailed("timeout"))
    use_case, _, _, _, telemetry = make(stock, Profile.DEADLINE_RETRY)
    fresh = await use_case("sku-001", None)
    degraded = await use_case("sku-001", None)
    assert (degraded.price, degraded.degraded, degraded.stale) == (fresh.price, True, True)
    assert telemetry.fallbacks == ["timeout"]


async def test_with_nothing_cached_the_base_price_is_returned_flagged_stale():
    use_case, _, _, _, _ = make(Stock(AttemptFailed("status 500")), Profile.DEADLINE_RETRY)
    quote = await use_case("sku-009", None)
    assert (quote.price, quote.in_stock, quote.degraded, quote.stale) == (base_price("sku-009"), None, True, True)


async def test_the_breaker_opens_after_repeated_failures_and_then_stock_is_not_called():
    stock = Stock(AttemptFailed("timeout"))
    use_case, _, _, breaker, telemetry = make(stock)
    for _ in range(4):
        await use_case("sku-001", None)
    assert breaker.state is BreakerState.OPEN
    calls_before = len(stock.calls)
    quote = await use_case("sku-001", None)
    assert len(stock.calls) == calls_before
    assert quote.degraded
    assert telemetry.rejected == 1
    assert telemetry.fallbacks[-1] == "breaker open"


async def test_after_the_cool_down_probes_that_succeed_close_the_breaker_again():
    stock = Stock(
        AttemptFailed("timeout"), AttemptFailed("timeout"), AttemptFailed("timeout"), AttemptFailed("timeout"), 5
    )
    use_case, clock, _, breaker, telemetry = make(stock)
    for _ in range(4):
        await use_case("sku-001", None)
    clock.now += 5.1
    await use_case("sku-001", None)
    await use_case("sku-001", None)
    assert breaker.state is BreakerState.CLOSED
    assert telemetry.states[-1] is BreakerState.CLOSED


async def test_the_deadline_retry_profile_has_no_breaker_and_keeps_calling_stock():
    stock = Stock(AttemptFailed("timeout"))
    use_case, _, _, breaker, _ = make(stock, Profile.DEADLINE_RETRY)
    for _ in range(8):
        await use_case("sku-001", None)
    assert len(stock.calls) == 8
    assert breaker.state is BreakerState.CLOSED


async def test_the_naive_profile_tries_three_times_without_a_deadline_and_then_fails_upward():
    stock = Stock(AttemptFailed("status 500"))
    use_case, _, _, _, _ = make(stock, Profile.NAIVE)
    with pytest.raises(StockUnavailable):
        await use_case("sku-001", Deadline.after(100.0, 2.0))
    assert [(t, sent, n) for t, sent, n in stock.calls] == [(2.0, False, 1), (2.0, False, 2), (2.0, False, 3)]


async def test_the_naive_profile_returns_a_fresh_quote_when_a_retry_succeeds():
    stock = Stock(AttemptFailed("status 500"), 5)
    use_case, _, _, _, _ = make(stock, Profile.NAIVE)
    assert (await use_case("sku-001", None)).degraded is False
    assert len(stock.calls) == 2


def test_the_cache_expires_entries_and_evicts_the_oldest_when_full():
    clock = Clock()
    cache = InMemoryQuoteCache(clock, ttl_seconds=10, max_entries=2)
    for sku in ("a", "b", "c"):
        cache.put(Quote(sku, Decimal("1"), True))
    assert cache.get("a") is None
    assert cache.get("c") is not None
    clock.now += 11
    assert cache.get("c") is None
