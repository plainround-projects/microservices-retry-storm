"""Prometheus metrics for one service: the counters the lesson reads amplification from, and a ``/metrics`` route."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class ServiceMetrics:
    """A registry with the metrics every service has; services add their own with :meth:`counter` and :meth:`gauge`.

    ``calls_received_total`` counts every business request that arrives, including the ones refused at once, so the
    ratio of two services' counters over the same window is the amplification between them.
    """

    def __init__(self, service: str) -> None:
        self.service = service
        self.registry = CollectorRegistry()
        self.received = Counter("calls_received_total", "Requests received", ["route"], registry=self.registry)
        self.sent = Counter("calls_sent_total", "Attempts sent to another service", ["target"], registry=self.registry)
        self.in_flight = Gauge("in_flight", "Requests being handled", registry=self.registry)
        self.duration = Histogram(
            "request_duration_seconds",
            "Time to handle a request",
            ["route", "status"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0),
            registry=self.registry,
        )

    def counter(self, name: str, documentation: str, labels: list[str] | None = None) -> Counter:
        """Register a counter in this service's registry."""
        return Counter(name, documentation, labels or [], registry=self.registry)

    def gauge(self, name: str, documentation: str, labels: list[str] | None = None) -> Gauge:
        """Register a gauge in this service's registry."""
        return Gauge(name, documentation, labels or [], registry=self.registry)


def metrics_router(metrics: ServiceMetrics) -> APIRouter:
    """``GET /metrics`` in the Prometheus text format."""
    router = APIRouter(tags=["metrics"])

    @router.get("/metrics")
    async def scrape() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return router
