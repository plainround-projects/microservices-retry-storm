"""Prometheus counters for the pricing service."""

from __future__ import annotations

from chassis.metrics import ServiceMetrics
from resilience.breaker import BreakerState


class PrometheusTelemetry:
    """Implements the ``Telemetry`` port with counters and a gauge in the service's registry."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._fallbacks = metrics.counter("fallbacks_total", "Degraded answers returned", ["reason"])
        self._rejected = metrics.counter("breaker_rejected_total", "Calls the open breaker refused locally")
        self._state = metrics.gauge("breaker_state", "0 closed, 1 open, 2 half-open")

    def fallback(self, reason: str) -> None:
        """A degraded answer was returned."""
        kind = reason if reason in {"breaker open", "timeout"} else "error"
        self._fallbacks.labels(kind).inc()

    def breaker_rejected(self) -> None:
        """The breaker refused to call the stock store."""
        self._rejected.inc()
        self._state.set(int(BreakerState.OPEN))

    def breaker_state(self, state: BreakerState) -> None:
        """The breaker's state, after it changed."""
        self._state.set(int(state))
