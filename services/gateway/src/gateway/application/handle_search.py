"""Handle a search request at the edge of the chain."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from gateway.application.ports import SearchUpstream, Telemetry
from gateway.domain.models import SearchAnswer
from resilience.backoff import Backoff
from resilience.deadline import Deadline
from resilience.profile import Profile
from resilience.retry import RetriesExhausted, run_budgeted, run_naive
from resilience.retry_budget import RetryBudget

END_TO_END_DEADLINE = 2.0
HOP_TIMEOUT = 1.8


class Unavailable(Exception):
    """The gateway could not get an answer inside the policy: the client should come back after ``retry_after``."""

    def __init__(self, reason: str, retry_after: float = 1.0) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class HandleSearch:
    """What the gateway does depends on the profile.

    - ``naive``: three immediate attempts with a 2 second timeout each, no deadline passed down.
    - ``deadline-retry`` and ``full``: the request gets a 2 second deadline that travels in a header; the gateway
      retries (up to three attempts) with exponential backoff and full jitter, only while the retry budget (10 percent
      of requests) and the remaining time allow it. No other service retries.
    """

    def __init__(
        self,
        search: SearchUpstream,
        profile: Callable[[], Profile],
        budget: RetryBudget,
        backoff: Backoff,
        clock: Callable[[], float],
        telemetry: Telemetry,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._search = search
        self._profile = profile
        self._budget = budget
        self._backoff = backoff
        self._clock = clock
        self._telemetry = telemetry
        self._sleep = sleep
        self._rand = rand

    async def __call__(self, query: str) -> SearchAnswer:
        """Return the answer or raise :class:`Unavailable`."""
        counter = {"n": 0}

        async def attempt(timeout: float, deadline: Deadline | None) -> SearchAnswer:
            counter["n"] += 1
            return await self._search.search(query, timeout, deadline, counter["n"])

        try:
            if self._profile() is Profile.NAIVE:
                answer = await run_naive(lambda timeout: attempt(timeout, None))
            else:
                deadline = Deadline.after(self._clock(), END_TO_END_DEADLINE)
                answer = await run_budgeted(
                    lambda timeout: attempt(timeout, deadline),
                    deadline=deadline,
                    budget=self._budget,
                    backoff=self._backoff,
                    clock=self._clock,
                    sleep=self._sleep,
                    rand=self._rand,
                    hop_timeout=HOP_TIMEOUT,
                    on_retry=lambda _n, reason: self._telemetry.retry(reason),
                )
        except RetriesExhausted as error:
            self._telemetry.outcome("unavailable")
            raise Unavailable(error.why) from error
        self._telemetry.outcome("degraded" if answer.degraded else "ok")
        return answer
