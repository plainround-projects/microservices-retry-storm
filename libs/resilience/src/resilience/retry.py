"""Retry loops: the naive one the chain starts with, and the budgeted one only the edge is allowed to use."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from resilience.backoff import Backoff
from resilience.deadline import Deadline
from resilience.retry_budget import RetryBudget


class AttemptFailed(Exception):
    """One attempt failed. ``retryable`` says whether trying again can help; ``retry_after`` is the server's hint."""

    def __init__(self, reason: str, *, retryable: bool = True, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.retry_after = retry_after


class RetriesExhausted(Exception):
    """Every allowed attempt failed, or the deadline or the retry budget stopped further attempts."""

    def __init__(self, attempts: int, last: AttemptFailed, why: str) -> None:
        super().__init__(f"gave up after {attempts} attempt(s): {why} (last: {last.reason})")
        self.attempts = attempts
        self.last = last
        self.why = why


async def run_naive[T](attempt: Callable[[float], Awaitable[T]], *, attempts: int = 3, timeout: float = 2.0) -> T:
    """The chain's starting point: try again immediately on any error, with a fixed timeout and no backoff.

    Used at every hop of the naive chain, which is exactly why a slow bottom multiplies into a storm.
    """
    last = AttemptFailed("no attempt made")
    for _ in range(attempts):
        try:
            return await attempt(timeout)
        except AttemptFailed as error:
            last = error
    raise RetriesExhausted(attempts, last, "attempts used up")


async def run_budgeted[T](
    attempt: Callable[[float], Awaitable[T]],
    *,
    deadline: Deadline,
    budget: RetryBudget,
    backoff: Backoff,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float],
    max_attempts: int = 3,
    hop_timeout: float = 1.8,
    margin: float = 0.05,
    min_useful: float = 0.1,
    on_retry: Callable[[int, str], None] | None = None,
) -> T:
    """Try, and retry only if all of these hold: the error is retryable, the budget allows it, enough time is left.

    The pause between attempts is exponential with full jitter (or the server's ``Retry-After`` if it is longer),
    and the pause must itself fit in the remaining time. No attempt gets a timeout beyond what is left of the deadline.
    """
    budget.record_request()
    last = AttemptFailed("no attempt made")
    for number in range(1, max_attempts + 1):
        timeout = deadline.attempt_timeout(clock(), hop_timeout, margin)
        if timeout < min_useful:
            raise RetriesExhausted(number - 1, last, "deadline too short to try")
        try:
            return await attempt(timeout)
        except AttemptFailed as error:
            last = error
        if not last.retryable:
            raise RetriesExhausted(number, last, "error is not retryable")
        if number == max_attempts:
            break
        pause = max(backoff.delay(number, rand()), last.retry_after or 0.0)
        if deadline.remaining(clock()) - pause < min_useful + margin:
            raise RetriesExhausted(number, last, "not enough time left to retry")
        if not budget.try_acquire_retry():
            raise RetriesExhausted(number, last, "retry budget spent")
        if on_retry is not None:
            on_retry(number, last.reason)
        await sleep(pause)
    raise RetriesExhausted(max_attempts, last, "attempts used up")
