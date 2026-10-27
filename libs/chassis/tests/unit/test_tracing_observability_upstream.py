import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chassis.admin import admin_router
from chassis.deadline_ctx import current_deadline
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import (
    Span,
    SpanStore,
    child_of,
    current_span,
    new_trace,
    parse_traceparent,
)
from chassis.upstream import HopClient
from resilience.deadline import Deadline
from resilience.profile import ProfileHolder
from resilience.retry import AttemptFailed

VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def test_a_traceparent_header_round_trips_and_malformed_ones_are_ignored():
    context = parse_traceparent(VALID)
    assert context is not None
    assert (context.trace_id, context.span_id, context.sampled) == (
        "0af7651916cd43dd8448eb211c80319c",
        "b7ad6b7169203331",
        True,
    )
    assert context.header() == VALID
    assert parse_traceparent(VALID.replace("-01", "-00")).sampled is False  # type: ignore[union-attr]
    for bad in (None, "", "garbage", "00-" + "0" * 32 + "-b7ad6b7169203331-01", VALID.upper()):
        assert parse_traceparent(bad) is None


def test_a_child_span_keeps_the_trace_and_the_sampling_decision_and_has_a_new_id():
    parent = new_trace(True)
    child = child_of(parent)
    assert (child.trace_id, child.sampled) == (parent.trace_id, True)
    assert child.span_id != parent.span_id


def span(trace_id, name, start):
    return Span(trace_id, name, None, "svc", name, start, 1.0, "ok")


def test_the_span_store_keeps_the_newest_traces_and_orders_spans_by_start():
    store = SpanStore(max_traces=2, max_spans_per_trace=2)
    store.record(span("a", "second", 2.0))
    store.record(span("a", "first", 1.0))
    store.record(span("a", "dropped", 3.0))
    store.record(span("b", "b1", 1.0))
    store.record(span("c", "c1", 1.0))
    assert store.trace_ids() == ["b", "c"]
    assert [s.name for s in store.trace("a")] == []
    store2 = SpanStore(max_spans_per_trace=2)
    for name, start in (("second", 2.0), ("first", 1.0), ("dropped", 3.0)):
        store2.record(span("a", name, start))
    assert [s.name for s in store2.trace("a")] == ["first", "second"]
    assert store2.trace("a")[0].as_dict()["service"] == "svc"


def make_app(sample_rate=0.0):
    metrics = ServiceMetrics("demo")
    store = SpanStore()
    app = FastAPI()
    app.add_middleware(
        ObservabilityMiddleware, service="demo", metrics=metrics, store=store, sample_rate=sample_rate, rand=lambda: 0.0
    )
    app.include_router(metrics_router(metrics))
    holder = ProfileHolder()
    app.include_router(admin_router(holder, store))
    seen: dict[str, object] = {}

    @app.post("/work")
    async def work():
        seen["deadline"] = current_deadline()
        seen["span"] = current_span()
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=False), metrics, store, seen


def test_the_middleware_joins_the_callers_trace_reads_the_deadline_and_counts_the_request():
    client, _, store, seen = make_app()
    answer = client.post("/work", headers={"traceparent": VALID, "X-Deadline-Ms": "1500"})
    assert answer.headers["x-trace-id"] == "0af7651916cd43dd8448eb211c80319c"
    assert seen["span"].trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert 1.0 < seen["deadline"].remaining(__import__("time").monotonic()) <= 1.5
    spans = store.trace("0af7651916cd43dd8448eb211c80319c")
    assert [(s.name, s.parent_id, s.status) for s in spans] == [("demo /work", "b7ad6b7169203331", "200")]
    text = client.get("/metrics").text
    assert 'calls_received_total{route="/work"} 1.0' in text
    assert "in_flight 0.0" in text


def test_a_request_without_a_trace_starts_one_and_only_a_sampled_or_forced_one_is_stored():
    client, _, store, _ = make_app(sample_rate=0.0)
    client.post("/work")
    assert store.trace_ids() == []
    forced = client.post("/work", headers={"X-Trace-Force": "1"})
    assert store.trace_ids() == [forced.headers["x-trace-id"]]
    sampled_client, _, sampled_store, _ = make_app(sample_rate=0.5)
    sampled_client.post("/work")
    assert len(sampled_store.trace_ids()) == 1


def test_an_error_is_recorded_as_a_500_and_a_malformed_deadline_is_ignored():
    client, _, _, seen = make_app()
    assert client.get("/boom").status_code == 500
    client.post("/work", headers={"X-Deadline-Ms": "soon"})
    assert seen["deadline"] is None


def test_admin_routes_switch_the_profile_list_traces_and_reject_unknown_names():
    client, _, _, _ = make_app()
    assert client.post("/admin/profile", json={"name": "full"}).json() == {"name": "full"}
    assert client.get("/admin/profile").json() == {"name": "full"}
    assert client.post("/admin/profile", json={"name": "chaos"}).status_code == 422
    forced = client.post("/work", headers={"X-Trace-Force": "1"}).headers["x-trace-id"]
    assert client.get("/admin/traces").json() == [forced]
    assert client.get(f"/admin/traces/{forced}").json()[0]["name"] == "demo /work"


def hop(handler, deadline_clock=lambda: 100.0):
    metrics = ServiceMetrics("caller")
    store = SpanStore()
    inner = httpx.AsyncClient(base_url="http://down", transport=httpx.MockTransport(handler))
    return (
        HopClient("caller", "down", "http://down", metrics=metrics, store=store, client=inner, clock=deadline_clock),
        metrics,
        store,
    )


async def test_a_successful_call_sends_the_trace_and_deadline_headers_and_counts_the_attempt():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"quantity": 3})

    client, metrics, _ = hop(handler)
    answer = await client.call("/x", {"a": 1}, timeout=1.0, deadline=Deadline.after(100.0, 2.0))
    assert answer == {"quantity": 3}
    assert seen["traceparent"].startswith("00-")
    assert seen["x-deadline-ms"] == "2000"
    assert (
        'calls_sent_total{target="down"} 1.0'
        in __import__("prometheus_client").generate_latest(metrics.registry).decode()
    )


async def test_server_errors_and_timeouts_are_retryable_failures_and_client_errors_are_not():
    for status, retryable in ((500, True), (503, True), (429, True), (404, False)):
        client, _, _ = hop(lambda request, s=status: httpx.Response(s))
        with pytest.raises(AttemptFailed) as caught:
            await client.call("/x", {}, timeout=1.0, deadline=None)
        assert caught.value.retryable is retryable

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    client, _, store = hop(slow)
    parent = new_trace(True)
    from chassis.tracing import reset_current, set_current

    token = set_current(parent)
    try:
        with pytest.raises(AttemptFailed, match="timeout"):
            await client.call("/x", {}, timeout=0.05, deadline=None, attempt=2)
    finally:
        reset_current(token)
    recorded = store.trace(parent.trace_id)[0]
    assert (recorded.status, recorded.attributes["attempt"]) == ("timeout", 2)


async def test_a_connection_error_is_a_retryable_failure_and_retry_after_is_parsed():
    def refuse(request):
        raise httpx.ConnectError("refused")

    client, _, _ = hop(refuse)
    with pytest.raises(AttemptFailed, match="ConnectError"):
        await client.call("/x", {}, timeout=1.0, deadline=None)
    client, _, _ = hop(lambda request: httpx.Response(503, headers={"Retry-After": "2"}))
    with pytest.raises(AttemptFailed) as caught:
        await client.call("/x", {}, timeout=1.0, deadline=None)
    assert caught.value.retry_after == 2.0
    await client.close()
