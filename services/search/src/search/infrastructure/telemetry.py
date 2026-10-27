"""Prometheus counters for the search service."""

from __future__ import annotations

from chassis.metrics import ServiceMetrics


class PrometheusTelemetry:
    """Implements the ``Telemetry`` port."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._shed = metrics.counter("shed_total", "Requests refused by the concurrency limit")
        self._degraded = metrics.counter("degraded_total", "Partial results returned")

    def shed(self) -> None:
        """A request was refused because the concurrency limit was full."""
        self._shed.inc()

    def degraded(self) -> None:
        """A partial result was returned."""
        self._degraded.inc()
