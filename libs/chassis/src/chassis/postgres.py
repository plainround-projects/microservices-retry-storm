"""Async PostgreSQL pool with timeouts on everything, opened with retries while the database starts."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import cast

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

_log = logging.getLogger(__name__)

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
_SESSION_OPTIONS = "-c statement_timeout=3000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000"


async def create_pool(
    dsn: str, *, name: str, min_size: int = 2, max_size: int = 16, wait_seconds: float = 60.0
) -> Pool:
    """Open a pool of connections that return rows as dictionaries, waiting for the database with backoff."""
    deadline = time.monotonic() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=2.0,
            name=name,
            open=False,
            check=AsyncConnectionPool.check_connection,
            max_idle=120.0,
            kwargs={"row_factory": dict_row, "connect_timeout": 5, "options": _SESSION_OPTIONS},
        )
        try:
            await pool.open(wait=True, timeout=3.0)
            return cast(Pool, pool)
        except PoolTimeout:
            await pool.close()
            if time.monotonic() >= deadline:
                raise
            _log.warning("waiting for database", extra={"database": name, "attempt": attempt})
            await asyncio.sleep(min(2.0, 0.2 * 2**attempt))


async def check_pool(pool: Pool) -> None:
    """Readiness check: run ``SELECT 1`` on a pooled connection."""
    async with pool.connection() as connection:
        await connection.execute("SELECT 1")
