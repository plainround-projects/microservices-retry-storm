"""The composition root: build adapters, wire the use case, return the application."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.postgres import create_pool
from chassis.tracing import SpanStore
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults
from stock.infrastructure.fault_state import FaultState
from stock.infrastructure.postgres import PostgresStockRepository
from stock.infrastructure.settings import Settings
from stock.interfaces.api import create_app


async def build_app(settings: Settings) -> FastAPI:
    """Open the pool, wire the use case, and close the pool when the app shuts down."""
    pool = await create_pool(settings.database_url, name="stock")
    faults = FaultState(Faults(settings.latency_ms, settings.fail_rate))
    metrics = ServiceMetrics("stock")
    health = HealthRegistry()
    state = {"database": "ok"}

    async def refresh_database_check() -> None:
        while True:
            try:
                async with pool.connection() as connection:
                    await connection.execute("SELECT 1")
                state["database"] = "ok"
            except Exception:
                state["database"] = "failed"
            await asyncio.sleep(2.0)

    def database_ready() -> None:
        if state["database"] != "ok":
            raise ConnectionError("database")

    health.add("database", database_ready)
    use_case = CheckStock(
        PostgresStockRepository(pool),
        faults.get,
        asyncio.Semaphore(settings.capacity),
        clock=time.monotonic,
        rand=random.random,
    )
    app = create_app(use_case, faults, health, metrics, SpanStore())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        checker = asyncio.create_task(refresh_database_check())
        yield
        checker.cancel()
        await pool.close()

    app.router.lifespan_context = lifespan
    return app
