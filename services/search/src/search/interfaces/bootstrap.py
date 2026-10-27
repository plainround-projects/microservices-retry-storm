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
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import ProfileHolder
from search.application.search_products import SearchProducts
from search.infrastructure.http_pricing import HttpPricingUpstream
from search.infrastructure.settings import Settings
from search.infrastructure.telemetry import PrometheusTelemetry
from search.interfaces.api import create_app


def build_app(settings: Settings) -> FastAPI:
    """Wire the use case and close the HTTP client when the app shuts down."""
    metrics = ServiceMetrics("search")
    store = SpanStore()
    holder = ProfileHolder()
    client = HopClient("search", "pricing", settings.pricing_url, metrics=metrics, store=store)
    use_case = SearchProducts(
        HttpPricingUpstream(client),
        ConcurrencyLimiter(settings.concurrency_limit),
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
