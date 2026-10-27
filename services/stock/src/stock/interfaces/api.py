"""The stock store's HTTP API: check a sku, and the admin route that injects latency and errors."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

from chassis.admin import admin_router
from chassis.deadline_ctx import current_deadline
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from resilience.errors import DeadlineExceeded
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults, InjectedFailure, InvalidFaults, UnknownSku
from stock.infrastructure.fault_state import FaultState


class StockBody(BaseModel):
    """``POST /stock``."""

    sku: str


class FaultsBody(BaseModel):
    """``POST /admin/config``: any field left out keeps its value."""

    latency_ms: int | None = None
    fail_rate: float | None = None


def create_app(
    check_stock: CheckStock,
    fault_state: FaultState,
    health: HealthRegistry,
    metrics: ServiceMetrics,
    store: SpanStore,
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="stock", docs_url=None, redoc_url=None)
    app.add_middleware(ObservabilityMiddleware, service="stock", metrics=metrics, store=store)
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(None, store))

    @app.post("/stock")
    async def stock(body: StockBody) -> dict[str, Any]:
        try:
            answer = await check_stock(body.sku, current_deadline())
        except InjectedFailure as error:
            raise HTTPException(status_code=500, detail="simulated failure") from error
        except UnknownSku as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except DeadlineExceeded as error:
            raise HTTPException(status_code=504, detail="deadline exceeded") from error
        return {"sku": answer.sku, "inStock": answer.in_stock, "quantity": answer.quantity}

    admin = APIRouter(prefix="/admin", tags=["admin"])

    @admin.get("/config")
    async def read_config() -> dict[str, Any]:
        faults = fault_state.get()
        return {"latencyMs": faults.latency_ms, "failRate": faults.fail_rate}

    @admin.post("/config")
    async def set_config(body: FaultsBody) -> dict[str, Any]:
        current = fault_state.get()
        try:
            fault_state.set(
                Faults(
                    current.latency_ms if body.latency_ms is None else body.latency_ms,
                    current.fail_rate if body.fail_rate is None else body.fail_rate,
                )
            )
        except InvalidFaults as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return await read_config()

    app.include_router(admin)
    return app
