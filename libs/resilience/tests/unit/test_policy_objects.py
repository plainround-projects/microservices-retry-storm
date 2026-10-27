import pytest

from resilience.backoff import Backoff
from resilience.breaker import BreakerState, CircuitBreaker
from resilience.deadline import Deadline
from resilience.limiter import ConcurrencyLimiter
from resilience.retry_budget import RetryBudget


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_a_deadline_counts_down_and_never_goes_negative():
    deadline = Deadline.after(100.0, 2.0)
    assert deadline.remaining(100.5) == pytest.approx(1.5)
    assert deadline.expired(102.0)
    assert deadline.remaining(105.0) == 0.0
    assert deadline.header_value(100.5) == "1500"


def test_the_attempt_timeout_is_the_hop_limit_or_what_is_left_minus_the_margin():
    deadline = Deadline.after(0.0, 2.0)
    assert deadline.attempt_timeout(0.0, hop_limit=1.2, margin=0.05) == pytest.approx(1.2)
    assert deadline.attempt_timeout(1.5, hop_limit=1.2, margin=0.05) == pytest.approx(0.45)
    assert deadline.attempt_timeout(1.99, hop_limit=1.2, margin=0.05) == 0.0


@pytest.mark.parametrize(("value", "expected"), [("1500", 1.5), ("0", 0.0), ("999999999", 60.0), ("12.5", 0.0125)])
def test_a_deadline_header_is_parsed_and_clamped(value, expected):
    deadline = Deadline.from_header(value, now=10.0)
    assert deadline is not None
    assert deadline.remaining(10.0) == pytest.approx(expected)


@pytest.mark.parametrize("value", [None, "", "abc", "-5", "nan"])
def test_a_missing_or_malformed_deadline_header_is_ignored(value):
    assert Deadline.from_header(value, now=10.0) is None


def test_backoff_grows_exponentially_is_capped_and_is_scaled_by_the_random_draw():
    backoff = Backoff(base=0.05, cap=0.3, multiplier=2.0)
    assert [backoff.delay(n, 1.0) for n in (1, 2, 3, 4, 5)] == [0.05, 0.1, 0.2, 0.3, 0.3]
    assert backoff.delay(3, 0.0) == 0.0
    assert backoff.delay(3, 0.5) == pytest.approx(0.1)


def test_the_retry_budget_allows_ten_percent_of_the_requests_and_a_small_minimum():
    clock = Clock()
    budget = RetryBudget(clock, ratio=0.1, window_seconds=10, min_retries=3)
    assert [budget.try_acquire_retry() for _ in range(4)] == [True, True, True, False]
    for _ in range(100):
        budget.record_request()
    granted = sum(budget.try_acquire_retry() for _ in range(50))
    assert granted == 10 - 3
    assert budget.retries_in_window == 10
    assert budget.requests_in_window == 100


def test_the_retry_budget_forgets_old_events():
    clock = Clock()
    budget = RetryBudget(clock, ratio=0.1, window_seconds=10, min_retries=1)
    assert budget.try_acquire_retry() is True
    assert budget.try_acquire_retry() is False
    clock.advance(10.1)
    assert budget.try_acquire_retry() is True


def state_of(breaker: CircuitBreaker) -> BreakerState:
    return breaker.state


def make_breaker(clock):
    return CircuitBreaker(clock, failure_ratio=0.5, min_calls=10, window_seconds=10, open_seconds=5, half_open_probes=3)


def fail(breaker, n):
    for _ in range(n):
        assert breaker.allow()
        breaker.record_failure()


def test_the_breaker_stays_closed_below_the_minimum_number_of_calls_and_opens_at_the_failure_ratio():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 9)
    assert state_of(breaker) is BreakerState.CLOSED
    fail(breaker, 1)
    assert state_of(breaker) is BreakerState.OPEN
    assert breaker.allow() is False


def test_the_breaker_does_not_open_when_successes_keep_the_ratio_low():
    breaker = make_breaker(Clock())
    for _ in range(20):
        breaker.record_success()
        breaker.record_success()
        breaker.record_failure()
    assert state_of(breaker) is BreakerState.CLOSED


def test_after_the_cool_down_a_few_probes_are_allowed_and_success_closes_the_breaker():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 10)
    clock.advance(4.9)
    assert breaker.allow() is False
    clock.advance(0.2)
    assert state_of(breaker) is BreakerState.HALF_OPEN
    assert [breaker.allow() for _ in range(4)] == [True, True, True, False]
    for _ in range(3):
        breaker.record_success()
    assert state_of(breaker) is BreakerState.CLOSED
    assert breaker.allow() is True


def test_a_failed_probe_opens_the_breaker_again_for_a_full_cool_down():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 10)
    clock.advance(5.1)
    assert breaker.allow() is True
    breaker.record_failure()
    assert state_of(breaker) is BreakerState.OPEN
    clock.advance(4.0)
    assert breaker.allow() is False


def test_old_failures_age_out_of_the_window():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 9)
    clock.advance(11)
    breaker.record_failure()
    assert state_of(breaker) is BreakerState.CLOSED


def test_the_limiter_sheds_beyond_its_limit_and_frees_slots_on_release():
    limiter = ConcurrencyLimiter(2)
    assert [limiter.try_acquire() for _ in range(3)] == [True, True, False]
    assert limiter.in_flight == 2
    limiter.release()
    assert limiter.try_acquire() is True
    limiter.release()
    limiter.release()
    limiter.release()
    assert limiter.in_flight == 0
    assert limiter.limit == 2


def test_a_limiter_needs_a_positive_limit():
    with pytest.raises(ValueError, match="at least 1"):
        ConcurrencyLimiter(0)
