"""Trace context in the W3C ``traceparent`` format, and spans kept in memory for the sampled traces."""

from __future__ import annotations

import contextvars
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TRACEPARENT = "traceparent"
FORCE_HEADER = "X-Trace-Force"
_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True, slots=True)
class SpanContext:
    """Who a span is: its trace, its own id, and whether the trace is being recorded."""

    trace_id: str
    span_id: str
    sampled: bool

    def header(self) -> str:
        """The ``traceparent`` value to send to the next hop."""
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"


def parse_traceparent(value: str | None) -> SpanContext | None:
    """Parse a ``traceparent`` header; return ``None`` when it is missing or malformed."""
    if value is None:
        return None
    match = _PATTERN.match(value.strip())
    if match is None or set(match.group(1)) == {"0"}:
        return None
    return SpanContext(match.group(1), match.group(2), bool(int(match.group(3), 16) & 1))


def new_trace(sampled: bool) -> SpanContext:
    """Start a trace."""
    return SpanContext(secrets.token_hex(16), secrets.token_hex(8), sampled)


def child_of(parent: SpanContext) -> SpanContext:
    """A new span in the same trace."""
    return SpanContext(parent.trace_id, secrets.token_hex(8), parent.sampled)


@dataclass(frozen=True, slots=True)
class Span:
    """One finished unit of work: a request served here, or a call made from here."""

    trace_id: str
    span_id: str
    parent_id: str | None
    service: str
    name: str
    start_ms: float
    duration_ms: float
    status: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for the trace endpoint."""
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentId": self.parent_id,
            "service": self.service,
            "name": self.name,
            "startMs": round(self.start_ms, 3),
            "durationMs": round(self.duration_ms, 3),
            "status": self.status,
            "attributes": self.attributes,
        }


class SpanStore:
    """Keeps the spans of the most recent ``max_traces`` sampled traces, so memory stays bounded under load."""

    def __init__(self, max_traces: int = 300, max_spans_per_trace: int = 500) -> None:
        self._traces: OrderedDict[str, list[Span]] = OrderedDict()
        self._max_traces = max_traces
        self._max_spans = max_spans_per_trace

    def record(self, span: Span) -> None:
        """Store a span of a sampled trace."""
        spans = self._traces.get(span.trace_id)
        if spans is None:
            spans = self._traces[span.trace_id] = []
            while len(self._traces) > self._max_traces:
                self._traces.popitem(last=False)
        if len(spans) < self._max_spans:
            spans.append(span)

    def trace(self, trace_id: str) -> list[Span]:
        """The spans stored for the trace, oldest first."""
        return sorted(self._traces.get(trace_id, []), key=lambda s: s.start_ms)

    def trace_ids(self) -> list[str]:
        """Stored trace ids, oldest first."""
        return list(self._traces)


_current: contextvars.ContextVar[SpanContext | None] = contextvars.ContextVar("span_context", default=None)


def current_span() -> SpanContext | None:
    """The span being handled in this task, if any."""
    return _current.get()


def set_current(context: SpanContext | None) -> contextvars.Token[SpanContext | None]:
    """Make ``context`` current; returns the token to restore the previous value."""
    return _current.set(context)


def reset_current(token: contextvars.Token[SpanContext | None]) -> None:
    """Restore the previous span."""
    _current.reset(token)


class SpanRecorder:
    """Times a block and records a span for it when the trace is sampled."""

    def __init__(
        self,
        store: SpanStore,
        service: str,
        name: str,
        context: SpanContext,
        parent_id: str | None,
        attributes: dict[str, Any] | None = None,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.status = "ok"
        self._store = store
        self._service = service
        self._name = name
        self._parent_id = parent_id
        self._wall = wall
        self._mono = mono
        self._start_wall = 0.0
        self._start_mono = 0.0

    def __enter__(self) -> SpanRecorder:
        self._start_wall = self._wall()
        self._start_mono = self._mono()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc is not None and self.status == "ok":
            self.status = type(exc).__name__
        if self.context.sampled:
            self._store.record(
                Span(
                    self.context.trace_id,
                    self.context.span_id,
                    self._parent_id,
                    self._service,
                    self._name,
                    self._start_wall * 1000.0,
                    (self._mono() - self._start_mono) * 1000.0,
                    self.status,
                    self.attributes,
                )
            )
