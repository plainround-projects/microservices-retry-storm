import pytest

from resilience.backoff import Backoff
from resilience.deadline import Deadline
from resilience.retry import AttemptFailed, RetriesExhausted, run_budgeted, run_naive
from resilience.retry_budget import RetryBudget


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def test_the_naive_loop_retries_immediately_three_times_with_the_same_timeout():
    seen = []

    async def attempt(timeout):
        seen.append(timeout)
        raise AttemptFailed("boom")

    with pytest.raises(RetriesExhausted) as caught:
        await run_naive(attempt)
    assert seen == [2.0, 2.0, 2.0]
    assert caught.value.attempts == 3


async def test_the_naive_loop_returns_the_first_success():
    results = iter([AttemptFailed("x"), "ok"])

    async def attempt(timeout):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    assert await run_naive(attempt) == "ok"


def budgeted(attempt, *, deadline_seconds=2.0, budget=None, clock=None, sleeps=None, **kwargs):
    clock = clock or Clock()
    sleeps = sleeps if sleeps is not None else []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    return run_budgeted(
        attempt,
        deadline=Deadline.after(clock(), deadline_seconds),
        budget=budget or RetryBudget(clock, ratio=0.1, min_retries=3),
        backoff=Backoff(base=0.05, cap=0.5),
        clock=clock,
        sleep=sleep,
        rand=lambda: 1.0,
        **kwargs,
    )


async def test_a_success_needs_no_retry_and_counts_as_one_request():
    clock = Clock()
    budget = RetryBudget(clock)

    async def attempt(timeout):
        return "ok"

    assert await budgeted(attempt, budget=budget, clock=clock) == "ok"
    assert budget.requests_in_window == 1
    assert budget.retries_in_window == 0


async def test_a_retryable_error_is_retried_with_a_backoff_pause_and_succeeds():
    calls = []
    sleeps: list[float] = []

    async def attempt(timeout):
        calls.append(timeout)
        if len(calls) < 3:
            raise AttemptFailed("500")
        return "ok"

    assert await budgeted(attempt, sleeps=sleeps) == "ok"
    assert len(calls) == 3
    assert sleeps == [0.05, 0.1]


async def test_each_attempt_gets_no_more_time_than_is_left_of_the_deadline():
    clock = Clock()
    seen = []

    async def attempt(timeout):
        seen.append(round(timeout, 3))
        clock.now += timeout
        raise AttemptFailed("slow")

    with pytest.raises(RetriesExhausted):
        await budgeted(attempt, clock=clock, deadline_seconds=2.0, hop_timeout=1.8)
    assert seen[0] == 1.8
    assert sum(seen) <= 2.0


async def test_a_non_retryable_error_stops_at_once():
    calls = []

    async def attempt(timeout):
        calls.append(1)
        raise AttemptFailed("400", retryable=False)

    with pytest.raises(RetriesExhausted, match="not retryable"):
        await budgeted(attempt)
    assert len(calls) == 1


async def test_when_the_budget_is_spent_the_next_failure_is_not_retried():
    clock = Clock()
    budget = RetryBudget(clock, ratio=0.0, min_retries=1)
    calls = []

    async def attempt(timeout):
        calls.append(1)
        raise AttemptFailed("500")

    with pytest.raises(RetriesExhausted, match="budget"):
        await budgeted(attempt, budget=budget, clock=clock)
    assert len(calls) == 2
    with pytest.raises(RetriesExhausted, match="budget"):
        await budgeted(attempt, budget=budget, clock=clock)
    assert len(calls) == 3


async def test_a_server_retry_after_longer_than_the_time_left_ends_the_retries():
    async def attempt(timeout):
        raise AttemptFailed("503", retry_after=5.0)

    with pytest.raises(RetriesExhausted, match="not enough time"):
        await budgeted(attempt)


async def test_a_server_retry_after_that_fits_is_honored():
    sleeps: list[float] = []
    calls = []

    async def attempt(timeout):
        calls.append(1)
        if len(calls) == 1:
            raise AttemptFailed("503", retry_after=0.4)
        return "ok"

    assert await budgeted(attempt, sleeps=sleeps) == "ok"
    assert sleeps == [0.4]


async def test_an_expired_deadline_refuses_to_try_at_all():
    calls = []

    async def attempt(timeout):
        calls.append(1)

    with pytest.raises(RetriesExhausted, match="deadline too short"):
        await budgeted(attempt, deadline_seconds=0.05)
    assert calls == []


async def test_on_retry_is_told_about_each_retry():
    reasons = []

    async def attempt(timeout):
        raise AttemptFailed("boom")

    with pytest.raises(RetriesExhausted):
        await budgeted(attempt, on_retry=lambda n, why: reasons.append((n, why)))
    assert reasons == [(1, "boom"), (2, "boom")]


def test_profile_holder_switches_profiles_and_errors_carry_their_hints():
    from resilience.errors import DeadlineExceeded, Overloaded
    from resilience.profile import Profile, ProfileHolder

    holder = ProfileHolder()
    assert holder.get() is Profile.NAIVE
    holder.set(Profile.FULL)
    assert holder.get() is Profile.FULL
    assert Overloaded(2.0).retry_after == 2.0
    assert isinstance(DeadlineExceeded(), Exception)
    assert [p.value for p in Profile] == ["naive", "deadline-retry", "full"]
