import time

import pytest
from fastapi.testclient import TestClient

from chassis.errors import ConfigError
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from pricing.application.quote_price import QuotePrice
from pricing.infrastructure.cache import InMemoryQuoteCache
from pricing.infrastructure.settings import Settings
from pricing.infrastructure.telemetry import PrometheusTelemetry
from pricing.interfaces.api import create_app
from resilience.breaker import CircuitBreaker
from resilience.profile import ProfileHolder


class Stock:
    def __init__(self, quantity=5, error=None):
        self.quantity = quantity
        self.error = error

    async def check(self, sku, timeout, deadline, attempt):
        if self.error:
            raise self.error
        return self.quantity


def client(stock):
    from resilience.retry import AttemptFailed  # noqa: F401

    holder = ProfileHolder()
    metrics = ServiceMetrics("pricing")
    clock = time.monotonic
    use_case = QuotePrice(
        stock, InMemoryQuoteCache(clock), CircuitBreaker(clock), holder.get, clock, PrometheusTelemetry(metrics)
    )
    return TestClient(create_app(use_case, holder, HealthRegistry(), metrics, SpanStore())), holder


def test_the_price_route_returns_the_quote():
    c, _ = client(Stock())
    body = c.post("/price", json={"sku": "sku-001"}).json()
    assert body["sku"] == "sku-001"
    assert (body["degraded"], body["stale"], body["inStock"]) == (False, False, True)


def test_in_the_naive_profile_a_dead_stock_store_is_a_502():
    from resilience.retry import AttemptFailed

    c, _ = client(Stock(error=AttemptFailed("status 500")))
    assert c.post("/price", json={"sku": "sku-001"}).status_code == 502


def test_in_the_full_profile_a_dead_stock_store_gives_a_degraded_200_and_the_breaker_state_is_exported():
    from resilience.retry import AttemptFailed

    c, holder = client(Stock(error=AttemptFailed("timeout")))
    assert c.post("/admin/profile", json={"name": "full"}).json() == {"name": "full"}
    body = c.post("/price", json={"sku": "sku-001"}).json()
    assert (body["degraded"], body["stale"]) == (True, True)
    assert 'fallbacks_total{reason="timeout"} 1.0' in c.get("/metrics").text
    assert holder.get().value == "full"


def test_an_expired_deadline_is_a_504_and_an_unknown_profile_is_a_422():
    c, _ = client(Stock())
    c.post("/admin/profile", json={"name": "deadline-retry"})
    assert c.post("/price", json={"sku": "sku-001"}, headers={"X-Deadline-Ms": "100"}).status_code == 504
    assert c.post("/admin/profile", json={"name": "chaos"}).status_code == 422
    assert c.get("/admin/profile").json() == {"name": "deadline-retry"}


def test_settings_defaults_and_validation():
    assert Settings.from_env({"STOCK_URL": "http://stock:8080"}).port == 8080
    with pytest.raises(ConfigError, match="STOCK_URL is required"):
        Settings.from_env({})
