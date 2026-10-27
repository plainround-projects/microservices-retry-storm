"""The deadline of the request being handled, set by the middleware from ``X-Deadline-Ms`` and read by the use cases."""

from __future__ import annotations

import contextvars

from resilience.deadline import Deadline

_current: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar("deadline", default=None)


def current_deadline() -> Deadline | None:
    """The caller's deadline, or ``None`` when the caller sent none (the naive chain)."""
    return _current.get()


def set_deadline(deadline: Deadline | None) -> contextvars.Token[Deadline | None]:
    """Make ``deadline`` current; returns the token to restore the previous value."""
    return _current.set(deadline)


def reset_deadline(token: contextvars.Token[Deadline | None]) -> None:
    """Restore the previous deadline."""
    _current.reset(token)
