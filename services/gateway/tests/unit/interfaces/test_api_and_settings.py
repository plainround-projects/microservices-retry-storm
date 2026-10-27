import time

import pytest
from fastapi.testclient import TestClient

from chassis.errors import ConfigError
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from gateway.application.handle_search import HandleSearch
from gateway.domain.models import SearchAnswer
from gateway.infrastructure.settings import Settings
from gateway.infrastructure.telemetry import PrometheusTelemetry
from gateway.interfaces.api import create_app
from resilience.backoff import Backoff
from resilience.profile import ProfileHolder
from resilience.retry import AttemptFailed
from resilience.retry_budget import RetryBudget


class Search:
    def __init__(self, error=None):
        self.error = error
        self.deadlines = []

    async def search(self, query, timeout, deadline, attempt):
        self.deadlines.append(deadline)
        if self.error:
            raise self.error
        return SearchAnswer(query, "sku-001", "9.99", True, False, False)


def client(search, sample_rate=0.0):
    holder = ProfileHolder()
    metrics = ServiceMetrics("gateway")
    store = SpanStore()

    async def no_sleep(seconds):
        return None

    handle = HandleSearch(
        search,
        holder.get,
        RetryBudget(time.monotonic),
        Backoff(),
        time.monotonic,
        PrometheusTelemetry(metrics),
        no_sleep,
        lambda: 1.0,
    )
    return TestClient(create_app(handle, holder, HealthRegistry(), metrics, store, sample_rate)), store


def test_a_search_returns_the_answer_and_a_trace_id_header():
    c, _ = client(Search())
    answer = c.post("/search", json={"query": "widget"})
    assert answer.json()["price"] == "9.99"
    assert len(answer.headers["x-trace-id"]) == 32


def test_when_the_chain_cannot_answer_the_client_gets_a_503_with_retry_after():
    c, _ = client(Search(AttemptFailed("status 500")))
    answer = c.post("/search", json={"query": "widget"})
    assert answer.status_code == 503
    assert answer.headers["retry-after"] == "1"


def test_a_forced_trace_is_stored_with_its_server_span_and_is_readable_through_the_admin_route():
    c, store = client(Search())
    trace_id = c.post("/search", json={"query": "widget"}, headers={"X-Trace-Force": "1"}).headers["x-trace-id"]
    assert [s.name for s in store.trace(trace_id)] == ["gateway /search"]
    assert c.get(f"/admin/traces/{trace_id}").json()[0]["service"] == "gateway"
    assert trace_id in c.get("/admin/traces").json()


def test_an_unsampled_trace_is_not_stored_unless_the_sample_rate_says_so():
    c, store = client(Search())
    c.post("/search", json={"query": "widget"})
    assert store.trace_ids() == []
    sampled, sampled_store = client(Search(), sample_rate=1.0)
    sampled.post("/search", json={"query": "widget"})
    assert len(sampled_store.trace_ids()) == 1


def test_settings_defaults_and_validation():
    assert Settings.from_env({"SEARCH_URL": "http://search:8080"}).trace_sample_rate == 0.0
    with pytest.raises(ConfigError, match="SEARCH_URL is required"):
        Settings.from_env({})
    with pytest.raises(ConfigError, match="TRACE_SAMPLE_RATE"):
        Settings.from_env({"SEARCH_URL": "x", "TRACE_SAMPLE_RATE": "3"})
