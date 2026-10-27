"""Helpers for the experiments: ports, admin calls, metrics, containers, the load generator and trace rendering."""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parents[2]
CHAIN = ("gateway", "search", "pricing", "stock")
_PORTS = {
    "gateway": ("GATEWAY_PORT", "43080"),
    "search": ("SEARCH_PORT", "43081"),
    "pricing": ("PRICING_PORT", "43082"),
    "stock": ("STOCK_PORT", "43083"),
}


def url(service: str) -> str:
    var, default = _PORTS[service]
    return f"http://127.0.0.1:{os.environ.get(var, default)}"


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=600
    )


async def wait_ready(services: tuple[str, ...] = CHAIN, timeout: float = 300) -> None:
    """Block until every service answers ``/health/ready``."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=10) as client:
        for service in services:
            while True:
                try:
                    if (await client.get(f"{url(service)}/health/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{service} did not become ready")
                await asyncio.sleep(1)


async def restart_chain() -> None:
    """Restart the four services so every counter, cache, breaker and queue starts empty (the database stays)."""
    compose("restart", "stock", "pricing", "search", "gateway")
    await wait_ready()


async def set_profile(name: str) -> None:
    """Switch every service (except stock, which has no profile) to a resilience profile."""
    async with httpx.AsyncClient(timeout=30) as client:
        for service in ("gateway", "search", "pricing"):
            answer = await client.post(f"{url(service)}/admin/profile", json={"name": name})
            answer.raise_for_status()


async def set_faults(latency_ms: int | None = None, fail_rate: float | None = None) -> None:
    """Change the latency and failure rate injected at the stock store."""
    body = {k: v for k, v in (("latency_ms", latency_ms), ("fail_rate", fail_rate)) if v is not None}
    async with httpx.AsyncClient(timeout=30) as client:
        (await client.post(f"{url('stock')}/admin/config", json=body)).raise_for_status()


async def scrape(service: str, client: httpx.AsyncClient | None = None) -> dict[str, float]:
    """The service's metrics as ``{"name{label=value,...}": value}``; an empty dict when it cannot be reached."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=5)
    try:
        text = (await http.get(f"{url(service)}/metrics")).text
    except httpx.HTTPError:
        return {}
    finally:
        if own:
            await http.aclose()
    values: dict[str, float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name.endswith(("_created", "_bucket")):
                continue
            labels = ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items()))
            values[f"{sample.name}{{{labels}}}" if labels else sample.name] = sample.value
    return values


def metric(values: dict[str, float], name: str, **labels: str) -> float:
    """One value out of :func:`scrape` output (0 when absent)."""
    key = f"{name}{{{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}}}" if labels else name
    return values.get(key, 0.0)


async def received_counts() -> dict[str, float]:
    """``calls_received_total`` of the business route of each service."""
    routes = {"gateway": "/search", "search": "/search", "pricing": "/price", "stock": "/stock"}
    return {s: metric(await scrape(s), "calls_received_total", route=routes[s]) for s in CHAIN}


@dataclass
class Sample:
    """One request as the load generator saw it."""

    started: float
    latency: float
    status: int
    degraded: bool = False


@dataclass
class LoadRecorder:
    """The record of a load run, with the statistics the experiments report for any window of it."""

    t0: float = 0.0
    samples: list[Sample] = field(default_factory=list)

    def window(self, start: float, end: float) -> dict[str, Any]:
        """Statistics for the requests that started between ``start`` and ``end`` seconds after the run began."""
        chosen = [s for s in self.samples if start <= s.started - self.t0 < end]
        if not chosen:
            return {
                "requests": 0,
                "p50_ms": None,
                "p99_ms": None,
                "error_rate": None,
                "degraded_rate": None,
                "statuses": {},
            }
        latencies = sorted(s.latency for s in chosen)
        errors = sum(1 for s in chosen if s.status != 200)
        degraded = sum(1 for s in chosen if s.status == 200 and s.degraded)
        return {
            "requests": len(chosen),
            "p50_ms": round(statistics.median(latencies) * 1000),
            "p99_ms": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))] * 1000),
            "error_rate": round(errors / len(chosen), 4),
            "degraded_rate": round(degraded / len(chosen), 4),
            "statuses": dict(Counter(s.status for s in chosen)),
        }


async def generate_load(recorder: LoadRecorder, rps: float, stop: asyncio.Event) -> None:
    """An open-loop load: one request every ``1/rps`` seconds whether or not earlier ones have finished."""
    limits = httpx.Limits(max_connections=5000, max_keepalive_connections=500)
    queries = [f"query {n}" for n in range(200)]
    async with httpx.AsyncClient(base_url=url("gateway"), limits=limits, timeout=15) as client:
        recorder.t0 = time.monotonic()
        tasks: set[asyncio.Task[None]] = set()

        async def one(n: int) -> None:
            started = time.monotonic()
            try:
                answer = await client.post("/search", json={"query": queries[n % len(queries)]})
                degraded = answer.status_code == 200 and bool(answer.json().get("degraded"))
                recorder.samples.append(Sample(started, time.monotonic() - started, answer.status_code, degraded))
            except httpx.HTTPError:
                recorder.samples.append(Sample(started, time.monotonic() - started, 0))

        n = 0
        while not stop.is_set():
            task = asyncio.create_task(one(n))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            n += 1
            next_at = recorder.t0 + n / rps
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
        if tasks:
            await asyncio.wait(tasks, timeout=30)


async def sample_stock_in_flight(into: list[tuple[float, float | None]], t0: float, stop: asyncio.Event) -> None:
    """Every half second, record the stock store's in-flight requests (``None`` when it cannot be reached)."""
    async with httpx.AsyncClient(timeout=3) as client:
        while not stop.is_set():
            values = await scrape("stock", client)
            into.append((time.monotonic() - t0, values.get("in_flight") if values else None))
            await asyncio.sleep(0.5)


def render_trace(spans: list[dict[str, Any]]) -> str:
    """An indented tree of the spans of one trace, with each span's start offset and duration in milliseconds."""
    if not spans:
        return "(no spans recorded)"
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    ids = {s["spanId"] for s in spans}
    for span in sorted(spans, key=lambda s: s["startMs"]):
        parent = span["parentId"] if span["parentId"] in ids else None
        by_parent.setdefault(parent, []).append(span)
    origin = min(s["startMs"] for s in spans)
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for span in by_parent.get(parent, []):
            attrs = ""
            if "attempt" in span["attributes"]:
                attrs = f" attempt={span['attributes']['attempt']}"
            lines.append(
                f"{'  ' * depth}{span['name']:<22} +{span['startMs'] - origin:8.1f} ms  {span['durationMs']:8.1f} ms  "
                f"[{span['status']}]{attrs}"
            )
            walk(span["spanId"], depth + 1)

    walk(None, 0)
    return "\n".join(lines)


async def fetch_trace(trace_id: str) -> list[dict[str, Any]]:
    """Collect the spans of one trace from all four services."""
    spans: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for service in CHAIN:
            try:
                spans.extend((await client.get(f"{url(service)}/admin/traces/{trace_id}")).json())
            except httpx.HTTPError:
                continue
    return spans


async def forced_request(query: str = "widget") -> tuple[str, dict[str, Any]]:
    """One gateway request with its trace forced on; returns the trace id and a summary of the response."""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as client:
        answer = await client.post(f"{url('gateway')}/search", json={"query": query}, headers={"X-Trace-Force": "1"})
    return answer.headers["x-trace-id"], {"status": answer.status_code, "seconds": round(time.monotonic() - started, 2)}
