"""The stock store reached over HTTP."""

from __future__ import annotations

from chassis.upstream import HopClient
from resilience.deadline import Deadline


class HttpStockUpstream:
    """One attempt at ``POST /stock``; the retry policy lives in the use case."""

    def __init__(self, client: HopClient) -> None:
        self._client = client

    async def check(self, sku: str, timeout: float, deadline: Deadline | None, attempt: int) -> int:
        """Units available for the sku."""
        answer = await self._client.call("/stock", {"sku": sku}, timeout=timeout, deadline=deadline, attempt=attempt)
        return int(answer["quantity"])
