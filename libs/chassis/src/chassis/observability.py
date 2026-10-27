"""One ASGI middleware for the cross-cutting concerns of a request: trace context, correlation id, deadline, metrics."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chassis.correlation import correlation_scope
from chassis.deadline_ctx import reset_deadline, set_deadline
from chassis.metrics import ServiceMetrics
from chassis.tracing import (
    FORCE_HEADER,
    TRACEPARENT,
    SpanRecorder,
    SpanStore,
    child_of,
    new_trace,
    parse_traceparent,
    reset_current,
    set_current,
)
from resilience.deadline import DEADLINE_HEADER, Deadline

_log = logging.getLogger("access")
_QUIET_PREFIXES = ("/health", "/metrics", "/admin")


class ObservabilityMiddleware:
    """For each business request: join or start a trace, make the trace id the correlation id, read the deadline header,
    count the request, time it, and record a server span when the trace is sampled.

    ``sample_rate`` only matters where a trace starts (the gateway); other services follow the caller's decision.
    A request with the ``X-Trace-Force`` header is always sampled.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str,
        metrics: ServiceMetrics,
        store: SpanStore,
        sample_rate: float = 0.0,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._service = service
        self._metrics = metrics
        self._store = store
        self._sample_rate = sample_rate
        self._rand = rand
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        path = scope["path"] if scope["type"] == "http" else ""
        if scope["type"] != "http" or path.startswith(_QUIET_PREFIXES):
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        parent = parse_traceparent(headers.get(TRACEPARENT))
        if parent is None:
            sampled = FORCE_HEADER.lower() in headers or self._rand() < self._sample_rate
            context = new_trace(sampled)
            parent_id = None
        else:
            context = child_of(parent)
            parent_id = parent.span_id
        now = self._clock()
        deadline = Deadline.from_header(headers.get(DEADLINE_HEADER.lower()), now)
        route = path
        status = 500
        self._metrics.received.labels(route).inc()
        self._metrics.in_flight.inc()
        token = set_current(context)
        deadline_token = set_deadline(deadline)
        started = self._clock()

        async def send_with_trace(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-trace-id", context.trace_id.encode()))
            await send(message)

        try:
            with (
                correlation_scope(context.trace_id),
                SpanRecorder(
                    self._store,
                    self._service,
                    f"{self._service} {route}",
                    context,
                    parent_id,
                    {"deadlineMs": None if deadline is None else int(deadline.remaining(now) * 1000)},
                ) as span,
            ):
                try:
                    await self._app(scope, receive, send_with_trace)
                finally:
                    span.status = str(status)
                    if context.sampled or status >= 500:
                        _log.info("request", extra=_fields(scope, status, self._clock() - started))
        finally:
            self._metrics.in_flight.dec()
            self._metrics.duration.labels(route, str(status)).observe(self._clock() - started)
            reset_deadline(deadline_token)
            reset_current(token)


def _fields(scope: Scope, status: int, seconds: float) -> dict[str, Any]:
    return {"method": scope["method"], "path": scope["path"], "status": status, "duration_ms": round(seconds * 1000, 1)}
