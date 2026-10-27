"""Pricing reached over HTTP."""

from __future__ import annotations

from chassis.upstream import HopClient
from resilience.deadline import Deadline
from search.domain.models import SearchResult


class HttpPricingUpstream:
    """One attempt at ``POST /price``; the retry policy lives in the use case."""

    def __init__(self, client: HopClient) -> None:
        self._client = client

    async def quote(
        self, query: str, sku: str, timeout: float, deadline: Deadline | None, attempt: int
    ) -> SearchResult:
        """The priced result for the sku."""
        answer = await self._client.call("/price", {"sku": sku}, timeout=timeout, deadline=deadline, attempt=attempt)
        return SearchResult(
            query, sku, answer["price"], answer["inStock"], bool(answer["degraded"]), bool(answer["stale"])
        )
