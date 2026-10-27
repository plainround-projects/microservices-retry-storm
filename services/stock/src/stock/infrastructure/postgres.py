"""PostgreSQL adapter for stock levels."""

from __future__ import annotations

from chassis.postgres import Pool


class PostgresStockRepository:
    """Reads ``stock_levels`` through the async pool."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def quantity(self, sku: str) -> int | None:
        """Units available, or ``None`` for an unknown sku."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute("SELECT quantity FROM stock_levels WHERE sku = %s", (sku,))
            row = await cursor.fetchone()
        return None if row is None else int(row["quantity"])
