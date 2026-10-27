"""Answer a stock check, slowly or wrongly when the experiment says so, honoring the caller's deadline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from stock.application.ports import StockRepository
from stock.domain.models import Faults, InjectedFailure, StockAnswer, UnknownSku


class CheckStock:
    """The stock store's one operation.

    Work is limited to ``capacity`` at a time (a store has finite connections and CPU). A request that arrives with a
    deadline gives up as soon as the deadline passes, whether it is still waiting for capacity or already
    "working", so abandoned requests stop costing anything. A request without a deadline (the naive chain) is carried
    through to the end even if its caller left long ago, which is how a backlog outlives the slowdown that built it.
    """

    def __init__(
        self,
        repository: StockRepository,
        faults: Callable[[], Faults],
        capacity: asyncio.Semaphore,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float],
    ) -> None:
        self._repository = repository
        self._faults = faults
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def __call__(self, sku: str, deadline: Deadline | None) -> StockAnswer:
        """Return the stock of ``sku``; raise ``InjectedFailure``, ``UnknownSku`` or ``DeadlineExceeded``."""
        if deadline is None:
            return await self._work(sku)
        remaining = deadline.remaining(self._clock())
        if remaining <= 0:
            raise DeadlineExceeded
        try:
            async with asyncio.timeout(remaining):
                return await self._work(sku)
        except TimeoutError as error:
            raise DeadlineExceeded from error

    async def _work(self, sku: str) -> StockAnswer:
        async with self._capacity:
            faults = self._faults()
            await self._sleep(faults.latency_ms / 1000.0)
            if self._rand() < faults.fail_rate:
                raise InjectedFailure
            quantity = await self._repository.quantity(sku)
        if quantity is None:
            raise UnknownSku(sku)
        return StockAnswer(sku, quantity)
