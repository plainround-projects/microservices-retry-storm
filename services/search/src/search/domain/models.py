"""The catalog lookup and the search result."""

from __future__ import annotations

import zlib
from dataclasses import dataclass

CATALOG_SIZE = 20


def resolve_sku(query: str) -> str:
    """Map a query to one of the 20 catalog skus (``sku-001`` to ``sku-020``), the same way every time."""
    normalized = " ".join(query.lower().split())
    return f"sku-{zlib.crc32(normalized.encode()) % CATALOG_SIZE + 1:03d}"


@dataclass(frozen=True, slots=True)
class SearchResult:
    """What a search returns. ``degraded`` means pricing could not be asked in time, so there is no price."""

    query: str
    sku: str
    price: str | None
    in_stock: bool | None
    degraded: bool
    stale: bool
