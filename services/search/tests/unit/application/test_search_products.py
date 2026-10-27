import pytest

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded, Overloaded
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import Profile
from resilience.retry import AttemptFailed
from search.application.search_products import PricingUnavailable, SearchProducts
from search.domain.models import CATALOG_SIZE, SearchResult, resolve_sku


class Pricing:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def quote(self, query, sku, timeout, deadline, attempt):
        self.calls.append((sku, timeout, deadline is not None, attempt))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class Telemetry:
    def __init__(self):
        self.shed_count = 0
        self.degraded_count = 0

    def shed(self):
        self.shed_count += 1

    def degraded(self):
        self.degraded_count += 1


def make(pricing, profile=Profile.FULL, limit=2):
    telemetry = Telemetry()
    return SearchProducts(pricing, ConcurrencyLimiter(limit), lambda: profile, lambda: 100.0, telemetry), telemetry


def ok(query="widget"):
    return SearchResult(query, resolve_sku(query), "9.99", True, False, False)


def test_a_query_resolves_to_the_same_catalog_sku_every_time_regardless_of_case_and_spacing():
    assert resolve_sku("Widget  Large") == resolve_sku("widget large")
    assert resolve_sku("widget") != resolve_sku("gadget") or CATALOG_SIZE == 1
    assert resolve_sku("anything").startswith("sku-")
    assert 1 <= int(resolve_sku("anything")[4:]) <= CATALOG_SIZE


async def test_the_priced_result_is_returned_with_one_attempt_inside_the_deadline():
    pricing = Pricing(ok())
    search, _ = make(pricing)
    result = await search("widget", Deadline.after(100.0, 2.0))
    assert result.price == "9.99"
    assert pricing.calls == [(resolve_sku("widget"), 1.2, True, 1)]


async def test_when_pricing_fails_a_partial_degraded_result_is_returned_instead_of_an_error():
    search, telemetry = make(Pricing(AttemptFailed("timeout")), Profile.DEADLINE_RETRY)
    result = await search("widget", None)
    assert (result.price, result.degraded) == (None, True)
    assert telemetry.degraded_count == 1


async def test_a_deadline_too_short_to_try_is_refused():
    pricing = Pricing(ok())
    search, _ = make(pricing)
    with pytest.raises(DeadlineExceeded):
        await search("widget", Deadline.after(100.0, 0.1))
    assert pricing.calls == []


async def test_beyond_the_concurrency_limit_the_full_profile_sheds_with_a_retry_hint():
    import asyncio

    release = asyncio.Event()

    class Slow:
        async def quote(self, query, sku, timeout, deadline, attempt):
            await release.wait()
            return ok(query)

    search, telemetry = make(Slow(), limit=2)
    running = [asyncio.create_task(search("a", None)) for _ in range(2)]
    await asyncio.sleep(0)
    with pytest.raises(Overloaded) as caught:
        await search("b", None)
    assert caught.value.retry_after == 1.0
    assert telemetry.shed_count == 1
    release.set()
    await asyncio.gather(*running)
    assert (await search("c", None)).degraded is False


async def test_the_other_profiles_never_shed():
    pricing = Pricing(ok())
    search, telemetry = make(pricing, Profile.DEADLINE_RETRY, limit=1)
    results = [await search("widget", None) for _ in range(3)]
    assert len(results) == 3
    assert telemetry.shed_count == 0


async def test_the_naive_profile_tries_three_times_with_two_second_timeouts_and_then_fails_upward():
    pricing = Pricing(AttemptFailed("status 500"))
    search, _ = make(pricing, Profile.NAIVE)
    with pytest.raises(PricingUnavailable):
        await search("widget", Deadline.after(100.0, 2.0))
    assert [(t, sent, n) for _, t, sent, n in pricing.calls] == [(2.0, False, 1), (2.0, False, 2), (2.0, False, 3)]
