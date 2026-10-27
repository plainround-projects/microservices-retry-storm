"""The lesson's experiments, one function per measurement, shared by the command line and the tests.

Every experiment restarts the four services first (empty counters, caches, breaker and queues), sets the resilience
profile and the stock store's faults, drives the gateway with an open-loop load, and reports what it measured.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Sequence
from typing import Any

import httpx

from tests.system import harness as h

RESULTS = h.ROOT / "docs" / "results"
RPS = float(os.environ.get("LOAD_RPS", "15"))


def _save(name: str, data: dict[str, Any], traces: dict[str, str] | None = None) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    for label, text in (traces or {}).items():
        (RESULTS / f"{name}-trace-{label}.txt").write_text(text + "\n")


async def _prepare(profile: str, latency_ms: int, fail_rate: float) -> None:
    await h.restart_chain()
    await h.set_profile(profile)
    await h.set_faults(latency_ms=latency_ms, fail_rate=fail_rate)
    await asyncio.sleep(2)


async def _start_load(rps: float) -> tuple[h.LoadRecorder, asyncio.Event, asyncio.Task[None]]:
    recorder, stop = h.LoadRecorder(), asyncio.Event()
    task = asyncio.create_task(h.generate_load(recorder, rps, stop))
    while recorder.t0 == 0.0:
        await asyncio.sleep(0.01)
    return recorder, stop, task


def _ratios(received: dict[str, float]) -> dict[str, float]:
    gateway = received["gateway"] or 1.0
    return {
        "search:gateway": round(received["search"] / gateway, 2),
        "pricing:gateway": round(received["pricing"] / gateway, 2),
        "stock:gateway": round(received["stock"] / gateway, 2),
    }


async def amplification(
    profile: str, fail_rate: float = 0.3, latency_ms: int = 50, seconds: int = 45
) -> dict[str, Any]:
    """Steps 2 and 4: count the calls arriving at each layer for the same number of gateway requests."""
    await _prepare(profile, latency_ms, fail_rate)
    before = await h.received_counts()
    recorder, stop, task = await _start_load(RPS)
    await asyncio.sleep(seconds)
    stop.set()
    await task
    await asyncio.sleep(5)
    after = await h.received_counts()
    received = {s: after[s] - before[s] for s in h.CHAIN}
    gateway_metrics = await h.scrape("gateway")
    result: dict[str, Any] = {
        "profile": profile,
        "fail_rate": fail_rate,
        "latency_ms": latency_ms,
        "rps": RPS,
        "received": received,
        "ratios": _ratios(received),
        "gateway_retries": sum(v for k, v in gateway_metrics.items() if k.startswith("retries_total{")),
        "client": recorder.window(0, seconds),
    }
    print(f"calls received over {seconds} s at {RPS:g} requests/s, profile {profile}, FAIL_RATE={fail_rate}:")
    print("   " + "   ".join(f"{s}: {received[s]:.0f}" for s in h.CHAIN))
    print(
        f"   ratio stock:gateway = {result['ratios']['stock:gateway']}x   (search {result['ratios']['search:gateway']}x, pricing {result['ratios']['pricing:gateway']}x)"
    )
    print(f"   client: {result['client']}")
    _save(f"amplification-{profile}", result)
    return result


def _recovered(
    recorder: h.LoadRecorder, in_flight: Sequence[tuple[float, float | None]], now: float, threshold: float
) -> bool:
    stats = recorder.window(now - 5, now)
    flights = [v for t, v in in_flight if now - 5 <= t < now]
    return bool(
        stats["requests"]
        and stats["error_rate"] == 0
        and stats["degraded_rate"] == 0
        and stats["p99_ms"] <= 600
        and flights
        and all(v is not None and v <= threshold for v in flights)
    )


async def slowdown(
    profile: str,
    fail_rate: float = 0.3,
    slow_ms: int = 1500,
    baseline_s: int = 20,
    slow_s: int = 40,
    recover_cap_s: int = 240,
) -> dict[str, Any]:
    """Steps 3 and 6: the stock store slows down under steady load, then recovers; measure the top and the bottom."""
    await _prepare(profile, 50, fail_rate)
    recorder, stop, load = await _start_load(RPS)
    in_flight: list[tuple[float, float | None]] = []
    sampler = asyncio.create_task(h.sample_stock_in_flight(in_flight, recorder.t0, stop))

    def elapsed() -> float:
        return time.monotonic() - recorder.t0

    await asyncio.sleep(baseline_s)
    trace_baseline = await h.forced_request()
    t_slow = elapsed()
    await h.set_faults(latency_ms=slow_ms)
    await asyncio.sleep(slow_s / 2)
    during_task = asyncio.create_task(h.forced_request())
    await asyncio.sleep(slow_s / 2)
    t_reset = elapsed()
    await h.set_faults(latency_ms=50)
    during = await asyncio.wait_for(during_task, 60)
    baseline_peak = max((v for t, v in in_flight if 5 <= t < t_slow and v is not None), default=0.0)
    threshold = max(3 * baseline_peak, 10.0)
    recovered_at: float | None = None
    while elapsed() < t_reset + recover_cap_s:
        await asyncio.sleep(1)
        if _recovered(recorder, in_flight, elapsed(), threshold):
            recovered_at = elapsed() - t_reset
            break
    stop.set()
    await load
    await sampler
    during_flights = [v for t, v in in_flight if t_slow + 5 <= t < t_reset and v is not None]
    after_flights = [(round(t - t_reset), v) for t, v in in_flight if t >= t_reset and v is not None][::6]
    result: dict[str, Any] = {
        "profile": profile,
        "fail_rate": fail_rate,
        "slow_ms": slow_ms,
        "rps": RPS,
        "baseline": recorder.window(5, t_slow),
        "slowdown": recorder.window(t_slow + 5, t_reset),
        "stock_in_flight": {
            "baseline_peak": baseline_peak,
            "slowdown_peak": max(during_flights, default=0),
            "after_reset_every_3s": after_flights,
        },
        "recovery_seconds": None if recovered_at is None else round(recovered_at, 1),
        "recovery_capped_at_s": recover_cap_s,
        "recovery_threshold_in_flight": threshold,
        "traces": {"baseline": trace_baseline[1], "slowdown": during[1]},
    }
    traces = {
        "baseline": h.render_trace(await h.fetch_trace(trace_baseline[0])),
        "slowdown": h.render_trace(await h.fetch_trace(during[0])),
    }
    result["stock_spans_in_slowdown_trace"] = (
        traces["slowdown"].count("call stock") if profile == "naive" else traces["slowdown"].count("stock /stock")
    )
    print(
        f"profile {profile}: FAIL_RATE={fail_rate}, stock latency 50 ms -> {slow_ms} ms for {slow_s} s, then back, at {RPS:g} requests/s"
    )
    print(f"   baseline : {result['baseline']}")
    print(f"   slowdown : {result['slowdown']}")
    print(
        f"   stock in-flight: baseline peak {baseline_peak:g}, slowdown peak {result['stock_in_flight']['slowdown_peak']:g}"
    )
    print(
        f"   recovery after the latency was removed: {result['recovery_seconds']} s (cap {recover_cap_s} s, in-flight threshold {threshold:g})"
    )
    _save(f"slowdown-{profile}", result, traces)
    return result


async def outage(seconds_before: int = 15, seconds_down: int = 30) -> dict[str, Any]:
    """Step 5: stop the stock store entirely under the full profile; the gateway must keep answering inside its deadline."""
    await _prepare("full", 50, 0.0)
    recorder, stop, load = await _start_load(RPS)
    await asyncio.sleep(seconds_before)
    pricing_before = await h.scrape("pricing")
    h.compose("stop", "stock")
    t_down = time.monotonic() - recorder.t0
    await asyncio.sleep(seconds_down)
    pricing_during = await h.scrape("pricing")
    trace_id, summary = await h.forced_request()
    trace = h.render_trace(await h.fetch_trace(trace_id))
    t_up = time.monotonic() - recorder.t0
    h.compose("start", "stock")
    await h.wait_ready(("stock",))
    await asyncio.sleep(12)
    stop.set()
    await load
    sent = h.metric(pricing_during, "calls_sent_total", target="stock") - h.metric(
        pricing_before, "calls_sent_total", target="stock"
    )
    rejected = h.metric(pricing_during, "breaker_rejected_total") - h.metric(pricing_before, "breaker_rejected_total")
    requests_during = recorder.window(t_down, t_up)["requests"]
    result = {
        "rps": RPS,
        "before_outage": recorder.window(3, t_down),
        "during_outage": recorder.window(t_down + 6, t_up),
        "after_restart": recorder.window(t_up + 8, t_up + 12),
        "requests_during_outage": requests_during,
        "pricing_attempts_sent_to_stock_during_outage": sent,
        "pricing_calls_refused_by_the_breaker": rejected,
        "breaker_state_at_end_of_outage": h.metric(pricing_during, "breaker_state"),
        "forced_request": summary,
    }
    print(f"stock stopped for {seconds_down} s at {RPS:g} requests/s, full profile")
    print(f"   before   : {result['before_outage']}")
    print(f"   outage   : {result['during_outage']}")
    print(
        f"   attempts pricing sent to the dead stock store: {sent:.0f} of {requests_during} requests; refused locally by the open breaker: {rejected:.0f}"
    )
    print(f"   after    : {result['after_restart']}")
    _save("outage-full", result, {"outage": trace})
    return result


async def shedding(rps: float = 30.0, limit: int = 8, seconds: int = 30) -> dict[str, Any]:
    """Step 5, second half: with search's concurrency limit small and pricing slow, the excess is refused at once."""
    os.environ["SEARCH_CONCURRENCY_LIMIT"] = str(limit)
    h.compose("up", "-d", "--force-recreate", "--no-deps", "search")
    try:
        await _prepare("full", 1500, 0.0)
        await h.set_profile("full")
        # Take the breaker out of the picture so pricing keeps calling the slow store and search's in-flight count climbs.
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{h.url('pricing')}/admin/profile", json={"name": "deadline-retry"})
        recorder, stop, load = await _start_load(rps)
        await asyncio.sleep(seconds)
        stop.set()
        await load
        search_metrics = await h.scrape("search")
        gateway_metrics = await h.scrape("gateway")
        window = recorder.window(5, seconds)
        result = {
            "rps": rps,
            "limit": limit,
            "client": window,
            "search_shed": h.metric(search_metrics, "shed_total"),
            "gateway_retries": sum(v for k, v in gateway_metrics.items() if k.startswith("retries_total{")),
            "gateway_outcomes": {k: v for k, v in gateway_metrics.items() if k.startswith("outcomes_total{")},
        }
    finally:
        os.environ.pop("SEARCH_CONCURRENCY_LIMIT", None)
        h.compose("up", "-d", "--force-recreate", "--no-deps", "search")
        await h.wait_ready(("search",))
    print(f"search limited to {limit} in flight, pricing slow, {rps:g} requests/s for {seconds} s")
    print(
        f"   shed by search: {result['search_shed']:.0f}; gateway retries: {result['gateway_retries']:.0f}; client view: {window}"
    )
    _save("shedding-full", result)
    return result
