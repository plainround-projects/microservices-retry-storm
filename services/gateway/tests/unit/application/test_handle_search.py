import pytest

from gateway.application.handle_search import HandleSearch, Unavailable
from gateway.domain.models import SearchAnswer
from resilience.backoff import Backoff
from resilience.profile import Profile
from resilience.retry import AttemptFailed
from resilience.retry_budget import RetryBudget


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Search:
    def __init__(self, *results, clock=None, spend=0.0):
        self.results = list(results)
        self.calls = []
        self.clock = clock
        self.spend = spend

    async def search(self, query, timeout, deadline, attempt):
        self.calls.append((timeout, deadline is not None, attempt))
        if self.clock:
            self.clock.now += self.spend
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class Telemetry:
    def __init__(self):
        self.retries = []
        self.outcomes = []

    def retry(self, reason):
        self.retries.append(reason)

    def outcome(self, outcome):
        self.outcomes.append(outcome)


def answer(degraded=False):
    return SearchAnswer("widget", "sku-001", "9.99", True, degraded, False)


def make(search, profile=Profile.DEADLINE_RETRY, clock=None, min_retries=3):
    clock = clock or Clock()
    telemetry = Telemetry()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    handle = HandleSearch(
        search,
        lambda: profile,
        RetryBudget(clock, ratio=0.1, min_retries=min_retries),
        Backoff(0.05, 0.5),
        clock,
        telemetry,
        sleep,
        lambda: 1.0,
    )
    return handle, telemetry, sleeps, clock


async def test_a_good_answer_passes_through_with_a_deadline_header_and_no_retry():
    search = Search(answer())
    handle, telemetry, _, _ = make(search)
    assert (await handle("widget")).price == "9.99"
    assert search.calls == [(1.8, True, 1)]
    assert telemetry.outcomes == ["ok"]


async def test_a_degraded_answer_is_reported_as_degraded_not_as_failure():
    handle, telemetry, _, _ = make(Search(answer(degraded=True)))
    assert (await handle("widget")).degraded is True
    assert telemetry.outcomes == ["degraded"]


async def test_a_failure_is_retried_with_backoff_and_counted():
    search = Search(AttemptFailed("status 503"), answer())
    handle, telemetry, sleeps, _ = make(search)
    assert (await handle("widget")).price == "9.99"
    assert sleeps == [0.05]
    assert telemetry.retries == ["status 503"]
    assert len(search.calls) == 2


async def test_when_retries_run_out_the_client_is_told_to_come_back_later():
    handle, telemetry, _, _ = make(Search(AttemptFailed("status 500")))
    with pytest.raises(Unavailable) as caught:
        await handle("widget")
    assert caught.value.retry_after == 1.0
    assert telemetry.outcomes == ["unavailable"]


async def test_the_retry_budget_caps_retries_when_everything_fails():
    search = Search(AttemptFailed("status 500"))
    handle, telemetry, _, _ = make(search, min_retries=1)
    for _ in range(20):
        with pytest.raises(Unavailable):
            await handle("widget")
    assert len(telemetry.retries) <= 3
    assert len(search.calls) <= 20 + 3


async def test_the_naive_profile_has_no_deadline_no_backoff_and_three_immediate_attempts():
    search = Search(AttemptFailed("status 500"))
    handle, telemetry, sleeps, _ = make(search, Profile.NAIVE)
    with pytest.raises(Unavailable):
        await handle("widget")
    assert search.calls == [(2.0, False, 1), (2.0, False, 2), (2.0, False, 3)]
    assert sleeps == []
    assert telemetry.retries == []
