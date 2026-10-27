import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from chassis.errors import ConfigError
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults
from stock.infrastructure.fault_state import FaultState
from stock.infrastructure.settings import Settings
from stock.interfaces.api import create_app


class Repo:
    async def quantity(self, sku):
        return {"sku-001": 7}.get(sku)


def client(faults=None, rand=0.5):
    state = FaultState(faults or Faults(0, 0.0))
    use_case = CheckStock(Repo(), state.get, asyncio.Semaphore(4), clock=time.monotonic, rand=lambda: rand)
    metrics = ServiceMetrics("stock")
    return TestClient(create_app(use_case, state, HealthRegistry(), metrics, SpanStore())), state


def test_a_known_sku_answers_200_with_the_quantity():
    c, _ = client()
    assert c.post("/stock", json={"sku": "sku-001"}).json() == {"sku": "sku-001", "inStock": True, "quantity": 7}


def test_an_unknown_sku_is_404_and_an_injected_failure_is_500():
    c, _ = client()
    assert c.post("/stock", json={"sku": "ghost"}).status_code == 404
    failing, _ = client(Faults(0, 1.0))
    assert failing.post("/stock", json={"sku": "sku-001"}).status_code == 500


def test_an_expired_deadline_header_is_a_504():
    c, _ = client()
    assert c.post("/stock", json={"sku": "sku-001"}, headers={"X-Deadline-Ms": "0"}).status_code == 504


def test_the_admin_route_changes_latency_and_fail_rate_and_keeps_what_is_not_sent():
    c, state = client()
    assert c.post("/admin/config", json={"latency_ms": 1500}).json() == {"latencyMs": 1500, "failRate": 0.0}
    assert c.post("/admin/config", json={"fail_rate": 0.3}).json() == {"latencyMs": 1500, "failRate": 0.3}
    assert state.get() == Faults(1500, 0.3)
    assert c.post("/admin/config", json={"fail_rate": 3}).status_code == 422
    assert c.get("/admin/config").json()["failRate"] == 0.3


def test_business_requests_are_counted_but_health_and_admin_calls_are_not():
    c, _ = client()
    c.post("/stock", json={"sku": "sku-001"})
    c.get("/health/live")
    c.get("/admin/config")
    text = c.get("/metrics").text
    assert 'calls_received_total{route="/stock"} 1.0' in text
    assert "health" not in text.split("calls_received_total")[1].splitlines()[0]


def test_the_response_carries_a_trace_id_and_a_forced_trace_is_stored():
    c, _ = client()
    answer = c.post("/stock", json={"sku": "sku-001"}, headers={"X-Trace-Force": "1"})
    assert len(answer.headers["x-trace-id"]) == 32


def test_settings_defaults_and_validation():
    settings = Settings.from_env({"DATABASE_URL": "postgresql://x"})
    assert (settings.capacity, settings.latency_ms, settings.fail_rate) == (16, 50, 0.0)
    with pytest.raises(ConfigError, match="DATABASE_URL is required"):
        Settings.from_env({})
    with pytest.raises(ConfigError, match="FAIL_RATE"):
        Settings.from_env({"DATABASE_URL": "x", "FAIL_RATE": "2"})
