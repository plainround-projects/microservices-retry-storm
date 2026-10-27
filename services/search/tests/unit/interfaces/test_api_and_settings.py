import time

import pytest
from fastapi.testclient import TestClient

from chassis.errors import ConfigError
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import ProfileHolder
from resilience.retry import AttemptFailed
from search.application.search_products import SearchProducts
from search.domain.models import SearchResult
from search.infrastructure.settings import Settings
from search.infrastructure.telemetry import PrometheusTelemetry
from search.interfaces.api import create_app


class Pricing:
    def __init__(self, error=None):
        self.error = error

    async def quote(self, query, sku, timeout, deadline, attempt):
        if self.error:
            raise self.error
        return SearchResult(query, sku, "9.99", True, False, False)


def client(pricing, limiter=None):
    holder = ProfileHolder()
    metrics = ServiceMetrics("search")
    use_case = SearchProducts(
        pricing, limiter or ConcurrencyLimiter(5), holder.get, time.monotonic, PrometheusTelemetry(metrics)
    )
    return TestClient(create_app(use_case, holder, HealthRegistry(), metrics, SpanStore())), holder


def test_search_returns_the_sku_and_price():
    c, _ = client(Pricing())
    body = c.post("/search", json={"query": "widget"}).json()
    assert (body["price"], body["degraded"]) == ("9.99", False)


def test_a_failing_pricing_is_a_502_in_the_naive_profile_and_a_partial_200_in_the_others():
    c, _ = client(Pricing(AttemptFailed("status 500")))
    assert c.post("/search", json={"query": "widget"}).status_code == 502
    c.post("/admin/profile", json={"name": "deadline-retry"})
    body = c.post("/search", json={"query": "widget"}).json()
    assert (body["price"], body["degraded"]) == (None, True)
    assert "degraded_total 1.0" in c.get("/metrics").text


def test_a_shed_request_is_a_503_with_retry_after():
    limiter = ConcurrencyLimiter(1)
    c, _ = client(Pricing(), limiter)
    c.post("/admin/profile", json={"name": "full"})
    assert limiter.try_acquire()
    shed = c.post("/search", json={"query": "widget"})
    assert (shed.status_code, shed.headers["retry-after"]) == (503, "1")
    assert "shed_total 1.0" in c.get("/metrics").text
    limiter.release()
    assert c.post("/search", json={"query": "widget"}).status_code == 200


def test_an_expired_deadline_is_a_504_and_a_blank_query_a_422():
    c, _ = client(Pricing())
    c.post("/admin/profile", json={"name": "deadline-retry"})
    assert c.post("/search", json={"query": "widget"}, headers={"X-Deadline-Ms": "100"}).status_code == 504
    assert c.post("/search", json={"query": ""}).status_code == 422


def test_settings_defaults_and_validation():
    assert Settings.from_env({"PRICING_URL": "http://pricing:8080"}).concurrency_limit == 40
    with pytest.raises(ConfigError, match="PRICING_URL is required"):
        Settings.from_env({})
