"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol


class StockRepository(Protocol):
    """Reads stock levels from the store's own database."""

    async def quantity(self, sku: str) -> int | None:
        """Units available, or ``None`` for an unknown sku."""
