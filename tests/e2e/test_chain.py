"""The chain across the running containers (make up first)."""

import asyncio

import httpx
import pytest

from tests.system import harness

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
async def clean_chain():
    await harness.restart_chain()
    await harness.set_profile("naive")
    await harness.set_faults(latency_ms=50, fail_rate=0.0)


async def search(client, **kwargs):
    return await client.post(f"{harness.url('gateway')}/search", json={"query": "widget"}, **kwargs)


async def test_a_request_crosses_all_four_services_and_the_trace_shows_one_call_per_hop():
    async with httpx.AsyncClient(timeout=30) as client:
        answer = await search(client, headers={"X-Trace-Force": "1"})
    assert answer.status_code == 200
    assert answer.json()["degraded"] is False
    tree = harness.render_trace(await harness.fetch_trace(answer.headers["x-trace-id"]))
    for expected in (
        "gateway /search",
        "call search",
        "search /search",
        "call pricing",
        "pricing /price",
        "call stock",
        "stock /stock",
    ):
        assert expected in tree


async def test_the_trace_id_is_the_correlation_id_in_every_service():
    async with httpx.AsyncClient(timeout=30) as client:
        trace_id = (await search(client, headers={"X-Trace-Force": "1"})).headers["x-trace-id"]
    for service in harness.CHAIN:
        assert (await client_get(f"{harness.url(service)}/admin/traces/{trace_id}")) != []


async def client_get(url):
    async with httpx.AsyncClient(timeout=15) as client:
        return (await client.get(url)).json()


@pytest.mark.parametrize("profile", ["naive", "deadline-retry", "full"])
async def test_every_profile_answers_a_healthy_chain_with_a_fresh_price(profile):
    await harness.set_profile(profile)
    async with httpx.AsyncClient(timeout=30) as client:
        answers = [await search(client) for _ in range(10)]
    assert all(a.status_code == 200 and a.json()["degraded"] is False for a in answers)


async def test_with_the_full_profile_and_a_dead_stock_store_the_gateway_still_answers_with_a_degraded_price():
    await harness.set_profile("full")
    harness.compose("stop", "stock")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            first = await search(client)
            answers = [await search(client) for _ in range(30)]
    finally:
        harness.compose("start", "stock")
        await harness.wait_ready(("stock",))
    assert first.status_code == 200
    assert all(a.status_code == 200 for a in answers)
    assert all(a.json()["degraded"] and a.json()["stale"] for a in answers)


async def test_the_stock_store_refuses_work_that_cannot_finish_inside_the_callers_deadline():
    await harness.set_faults(latency_ms=800)
    async with httpx.AsyncClient(timeout=30) as client:
        started = asyncio.get_running_loop().time()
        answer = await client.post(
            f"{harness.url('stock')}/stock", json={"sku": "sku-001"}, headers={"X-Deadline-Ms": "200"}
        )
        elapsed = asyncio.get_running_loop().time() - started
    assert answer.status_code == 504
    assert elapsed < 0.6


async def test_admin_routes_switch_the_profile_and_reject_unknown_ones():
    async with httpx.AsyncClient(timeout=30) as client:
        assert (await client.post(f"{harness.url('gateway')}/admin/profile", json={"name": "full"})).json() == {
            "name": "full"
        }
        assert (await client.post(f"{harness.url('gateway')}/admin/profile", json={"name": "chaos"})).status_code == 422
