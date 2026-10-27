"""Search reached over HTTP."""

from __future__ import annotations

from chassis.upstream import HopClient
from gateway.domain.models import SearchAnswer
from resilience.deadline import Deadline


class HttpSearchUpstream:
    """One attempt at ``POST /search``; the retry policy lives in the use case."""

    def __init__(self, client: HopClient) -> None:
        self._client = client

    async def search(self, query: str, timeout: float, deadline: Deadline | None, attempt: int) -> SearchAnswer:
        """The answer to the query."""
        answer = await self._client.call(
            "/search", {"query": query}, timeout=timeout, deadline=deadline, attempt=attempt
        )
        return SearchAnswer(
            query, answer["sku"], answer["price"], answer["inStock"], bool(answer["degraded"]), bool(answer["stale"])
        )
