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
from gateway.application.handle_search import HandleSearch
from gateway.infrastructure.http_search import HttpSearchUpstream
from gateway.infrastructure.settings import Settings
from gateway.infrastructure.telemetry import PrometheusTelemetry
from gateway.interfaces.api import create_app
from resilience.backoff import Backoff
from resilience.profile import ProfileHolder
from resilience.retry_budget import RetryBudget


def build_app(settings: Settings) -> FastAPI:
    """Wire the use case and close the HTTP client when the app shuts down."""
    metrics = ServiceMetrics("gateway")
    store = SpanStore()
    holder = ProfileHolder()
    client = HopClient("gateway", "search", settings.search_url, metrics=metrics, store=store)
    budget = RetryBudget(time.monotonic, ratio=0.1, window_seconds=10.0, min_retries=3)
    use_case = HandleSearch(
        HttpSearchUpstream(client),
        holder.get,
        budget,
        Backoff(base=0.05, cap=0.5),
        time.monotonic,
        PrometheusTelemetry(metrics),
    )
    app = create_app(use_case, holder, HealthRegistry(), metrics, store, settings.trace_sample_rate)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.close()

    app.router.lifespan_context = lifespan
    return app
