"""Search's HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from chassis.admin import admin_router
from chassis.deadline_ctx import current_deadline
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from resilience.errors import DeadlineExceeded, Overloaded
from resilience.profile import ProfileHolder
from search.application.search_products import PricingUnavailable, SearchProducts


class SearchBody(BaseModel):
    """``POST /search``."""

    query: str = Field(min_length=1, max_length=200)


def create_app(
    search: SearchProducts, holder: ProfileHolder, health: HealthRegistry, metrics: ServiceMetrics, store: SpanStore
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="search", docs_url=None, redoc_url=None)
    app.add_middleware(ObservabilityMiddleware, service="search", metrics=metrics, store=store)
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(holder, store))

    @app.post("/search", response_model=None)
    async def run_search(body: SearchBody) -> dict[str, Any] | JSONResponse:
        try:
            result = await search(body.query, current_deadline())
        except Overloaded as error:
            return JSONResponse(
                {"detail": "overloaded"}, status_code=503, headers={"Retry-After": str(int(error.retry_after))}
            )
        except DeadlineExceeded as error:
            raise HTTPException(status_code=504, detail="deadline exceeded") from error
        except PricingUnavailable as error:
            raise HTTPException(status_code=502, detail="pricing unavailable") from error
        return {
            "query": result.query,
            "sku": result.sku,
            "price": result.price,
            "inStock": result.in_stock,
            "degraded": result.degraded,
            "stale": result.stale,
        }

    return app
