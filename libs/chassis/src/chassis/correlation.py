"""The correlation id that follows one request across every service and every log line."""

from __future__ import annotations

import contextvars
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

CORRELATION_HEADER = "X-Correlation-Id"

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    """Return a fresh 32-character hexadecimal id."""
    return uuid.uuid4().hex


def sanitize_correlation_id(candidate: str | None) -> str:
    """Accept an incoming id only if it is short and printable-safe; otherwise mint a new one.

    The value ends up in log lines, so anything that could forge a log entry is replaced.
    """
    if candidate is not None and _SAFE_ID.match(candidate):
        return candidate
    return new_correlation_id()


def current_correlation_id() -> str | None:
    """Return the id of the request being handled in this context, if any."""
    return _current.get()


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Make ``correlation_id`` (or a new one) current for the duration of the block."""
    value = sanitize_correlation_id(correlation_id)
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)
