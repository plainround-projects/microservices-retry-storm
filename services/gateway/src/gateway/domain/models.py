"""What the gateway returns."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchAnswer:
    """The result of a search as the client sees it. ``degraded`` is the signal that part of the chain was bypassed."""

    query: str
    sku: str
    price: str | None
    in_stock: bool | None
    degraded: bool
    stale: bool
