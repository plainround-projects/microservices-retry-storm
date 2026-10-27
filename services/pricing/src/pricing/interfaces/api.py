"""Pricing's HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from chassis.admin import admin_router
from chassis.deadline_ctx import current_deadline
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from pricing.application.quote_price import QuotePrice, StockUnavailable
from resilience.errors import DeadlineExceeded
from resilience.profile import ProfileHolder


class PriceBody(BaseModel):
    """``POST /price``."""

    sku: str


def create_app(
    quote_price: QuotePrice, holder: ProfileHolder, health: HealthRegistry, metrics: ServiceMetrics, store: SpanStore
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="pricing", docs_url=None, redoc_url=None)
    app.add_middleware(ObservabilityMiddleware, service="pricing", metrics=metrics, store=store)
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(holder, store))

    @app.post("/price")
    async def price(body: PriceBody) -> dict[str, Any]:
        try:
            quote = await quote_price(body.sku, current_deadline())
        except DeadlineExceeded as error:
            raise HTTPException(status_code=504, detail="deadline exceeded") from error
        except StockUnavailable as error:
            raise HTTPException(status_code=502, detail="stock unavailable") from error
        return {
            "sku": quote.sku,
            "price": str(quote.price),
            "inStock": quote.in_stock,
            "degraded": quote.degraded,
            "stale": quote.stale,
        }

    return app
