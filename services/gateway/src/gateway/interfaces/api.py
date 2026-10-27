"""The gateway's HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from chassis.admin import admin_router
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from gateway.application.handle_search import HandleSearch, Unavailable
from resilience.profile import ProfileHolder


class SearchBody(BaseModel):
    """``POST /search``."""

    query: str = Field(min_length=1, max_length=200)


def create_app(
    handle: HandleSearch,
    holder: ProfileHolder,
    health: HealthRegistry,
    metrics: ServiceMetrics,
    store: SpanStore,
    sample_rate: float,
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="gateway", docs_url=None, redoc_url=None)
    app.add_middleware(
        ObservabilityMiddleware, service="gateway", metrics=metrics, store=store, sample_rate=sample_rate
    )
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(holder, store))

    @app.post("/search", response_model=None)
    async def search(body: SearchBody) -> dict[str, Any] | JSONResponse:
        try:
            answer = await handle(body.query)
        except Unavailable as error:
            return JSONResponse(
                {"detail": "temporarily unavailable", "reason": str(error)},
                status_code=503,
                headers={"Retry-After": str(int(error.retry_after))},
            )
        return {
            "query": answer.query,
            "sku": answer.sku,
            "price": answer.price,
            "inStock": answer.in_stock,
            "degraded": answer.degraded,
            "stale": answer.stale,
        }

    return app
