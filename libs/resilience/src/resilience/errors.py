"""Errors that cross service boundaries in the same way in every service."""

from __future__ import annotations


class DeadlineExceeded(Exception):
    """The request cannot finish inside its deadline, so the service refuses (or abandons) the work."""


class Overloaded(Exception):
    """The service is at its concurrency limit and sheds this request; ``retry_after`` is the hint for the caller."""

    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__("overloaded")
        self.retry_after = retry_after
