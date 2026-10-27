"""The lesson's mechanisms, end to end through the four real applications in one process (no Docker needed)."""

import asyncio

import pytest

from resilience.profile import Profile
from stock.domain.models import Faults
from tests.system.chain import build_chain, received


async def calls(chain, n):
    return [await chain.gateway.post("/search", json={"query": f"query {i}"}) for i in range(n)]


async def test_a_request_crosses_all_four_applications_under_one_trace_id_with_a_shrinking_deadline():
    chain = build_chain()
    chain.set_profile(Profile.DEADLINE_RETRY)
    answer = await chain.gateway.post("/search", json={"query": "widget"}, headers={"X-Trace-Force": "1"})
    assert answer.status_code == 200
    trace_id = answer.headers["x-trace-id"]
    spans = {s: chain.stores[s].trace(trace_id) for s in chain.stores}
    assert all(spans[s] for s in spans)
    deadlines = [spans[s][0].attributes["deadlineMs"] for s in ("search", "pricing", "stock")]
    assert all(d is not None for d in deadlines)
    assert deadlines[0] > deadlines[1] > deadlines[2]
    assert deadlines[0] <= 2000


async def test_with_naive_retries_everywhere_failures_at_the_bottom_multiply_hop_by_hop():
    chain = build_chain(faults=Faults(0, 1.0))
    chain.set_profile(Profile.NAIVE)
    answers = await calls(chain, 5)
    assert all(a.status_code == 503 for a in answers)
    counts = {s: received(chain, s) for s in ("gateway", "search", "pricing", "stock")}
    assert counts == {"gateway": 5.0, "search": 15.0, "pricing": 45.0, "stock": 135.0}


async def test_with_the_policy_the_same_failures_cost_one_call_per_request_below_the_gateway_and_degrade_instead_of_failing():
    chain = build_chain(faults=Faults(0, 1.0))
    chain.set_profile(Profile.DEADLINE_RETRY)
    answers = await calls(chain, 40)
    assert all(a.status_code == 200 and a.json()["degraded"] for a in answers)
    assert received(chain, "stock") == received(chain, "gateway") == 40.0


async def test_the_gateway_retry_budget_caps_retries_at_ten_percent_when_search_keeps_failing():
    chain = build_chain(faults=Faults(0, 1.0))
    chain.profiles["gateway"].set(Profile.DEADLINE_RETRY)
    # Search and pricing stay naive, so a dead stock store makes search answer 502 and the gateway has to decide on retries.
    answers = await calls(chain, 50)
    assert all(a.status_code == 503 for a in answers)
    assert received(chain, "search") <= 50 + max(3, int(0.1 * 50))


async def test_with_the_full_profile_the_breaker_opens_and_the_stock_store_stops_receiving_calls():
    chain = build_chain(faults=Faults(0, 1.0))
    chain.set_profile(Profile.FULL)
    answers = await calls(chain, 60)
    assert all(a.status_code == 200 and a.json()["degraded"] for a in answers)
    stock_calls = received(chain, "stock")
    assert 10 <= stock_calls <= 12
    assert chain.breaker is not None
    assert chain.breaker.state.name == "OPEN"


async def test_search_sheds_the_excess_beyond_its_limit_with_a_503_and_retry_after():
    chain = build_chain(faults=Faults(300, 0.0), search_limit=2)
    chain.set_profile(Profile.FULL)
    chain.profiles["pricing"].set(Profile.DEADLINE_RETRY)
    answers = await asyncio.gather(*[chain.gateway.post("/search", json={"query": f"q{i}"}) for i in range(12)])
    statuses = sorted(a.status_code for a in answers)
    assert statuses.count(200) >= 2
    shed = [a for a in answers if a.status_code == 503]
    assert shed
    assert all(a.headers["retry-after"] == "1" for a in shed)


@pytest.mark.parametrize("profile", [Profile.NAIVE, Profile.DEADLINE_RETRY, Profile.FULL])
async def test_a_healthy_chain_answers_every_profile_with_a_fresh_price(profile):
    chain = build_chain()
    chain.set_profile(profile)
    answers = await calls(chain, 5)
    assert all(a.status_code == 200 and not a.json()["degraded"] for a in answers)
