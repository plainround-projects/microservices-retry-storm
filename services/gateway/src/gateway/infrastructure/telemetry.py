"""Prometheus counters for the gateway."""

from __future__ import annotations

from chassis.metrics import ServiceMetrics


class PrometheusTelemetry:
    """Implements the ``Telemetry`` port."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._retries = metrics.counter("retries_total", "Retries issued by the gateway", ["reason"])
        self._outcomes = metrics.counter("outcomes_total", "How requests ended", ["outcome"])

    def retry(self, reason: str) -> None:
        """A retry was issued."""
        label = reason if reason in {"timeout", "status 500", "status 502", "status 503", "status 504"} else "other"
        self._retries.labels(label).inc()

    def outcome(self, outcome: str) -> None:
        """The request ended as ``ok``, ``degraded`` or ``unavailable``."""
        self._outcomes.labels(outcome).inc()
