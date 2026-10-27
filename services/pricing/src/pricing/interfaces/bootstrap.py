"""The composition root: build adapters, wire the use case, return the application."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from chassis.upstream import HopClient
from pricing.application.quote_price import QuotePrice
from pricing.infrastructure.cache import InMemoryQuoteCache
from pricing.infrastructure.http_stock import HttpStockUpstream
from pricing.infrastructure.settings import Settings
from pricing.infrastructure.telemetry import PrometheusTelemetry
from pricing.interfaces.api import create_app
from resilience.breaker import CircuitBreaker
from resilience.profile import ProfileHolder


def build_app(settings: Settings) -> FastAPI:
    """Wire the use case and close the HTTP client when the app shuts down."""
    metrics = ServiceMetrics("pricing")
    store = SpanStore()
    holder = ProfileHolder()
    client = HopClient("pricing", "stock", settings.stock_url, metrics=metrics, store=store)
    breaker = CircuitBreaker(time.monotonic, failure_ratio=0.5, min_calls=10, window_seconds=10.0, open_seconds=5.0)
    use_case = QuotePrice(
        HttpStockUpstream(client),
        InMemoryQuoteCache(time.monotonic),
        breaker,
        holder.get,
        time.monotonic,
        PrometheusTelemetry(metrics),
    )
    app = create_app(use_case, holder, HealthRegistry(), metrics, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.close()

    app.router.lifespan_context = lifespan
    return app
