"""One attempt at one call to another service: timeout, trace context, deadline header, counters. No retries."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

from chassis.metrics import ServiceMetrics
from chassis.tracing import TRACEPARENT, SpanRecorder, SpanStore, child_of, current_span, new_trace
from resilience.deadline import DEADLINE_HEADER, Deadline
from resilience.retry import AttemptFailed


class HopClient:
    """Calls one downstream service. The caller decides how many times to try; this class makes exactly one attempt.

    A failure is reported as :class:`AttemptFailed` with ``retryable`` set from the outcome: timeouts, connection
    errors and 5xx/429 answers are retryable, other 4xx answers are not. A ``Retry-After`` header becomes
    ``retry_after``.
    """

    def __init__(
        self,
        service: str,
        target: str,
        base_url: str,
        *,
        metrics: ServiceMetrics,
        store: SpanStore,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._target = target
        self._metrics = metrics
        self._store = store
        self._clock = clock
        limits = httpx.Limits(max_connections=2000, max_keepalive_connections=200)
        self._client = client or httpx.AsyncClient(base_url=base_url, limits=limits)

    async def call(
        self, path: str, body: dict[str, Any], *, timeout: float, deadline: Deadline | None, attempt: int = 1
    ) -> dict[str, Any]:
        """One POST of ``body``; return the JSON answer or raise :class:`AttemptFailed`."""
        self._metrics.sent.labels(self._target).inc()
        parent = current_span() or new_trace(False)
        context = child_of(parent)
        headers = {TRACEPARENT: context.header()}
        if deadline is not None:
            headers[DEADLINE_HEADER] = deadline.header_value(self._clock())
        with SpanRecorder(
            self._store,
            self._service,
            f"call {self._target}",
            context,
            parent.span_id,
            {"attempt": attempt, "timeoutMs": int(timeout * 1000)},
        ) as span:
            try:
                async with asyncio.timeout(timeout):
                    response = await self._client.post(path, json=body, headers=headers)
            except TimeoutError as error:
                span.status = "timeout"
                raise AttemptFailed("timeout") from error
            except httpx.TransportError as error:
                span.status = "connect-error"
                raise AttemptFailed(type(error).__name__) from error
            span.status = str(response.status_code)
            if response.status_code >= 500 or response.status_code == 429:
                raise AttemptFailed(f"status {response.status_code}", retry_after=_retry_after(response))
            if response.status_code >= 400:
                raise AttemptFailed(f"status {response.status_code}", retryable=False)
            answer: dict[str, Any] = response.json()
            return answer

    async def close(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None
