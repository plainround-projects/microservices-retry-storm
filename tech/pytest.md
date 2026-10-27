# pytest and pytest-cov

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

pytest is a tool for running tests written in Python. A **test** is a function that runs a piece of your code and checks the result with `assert`. If an `assert` is false, or an unexpected error is raised, the test fails. pytest finds the tests, runs them, and prints a summary. `pytest-cov` adds **coverage**: a report of which lines (and which branches of an `if`) the tests ran.

## Why this repository uses it

The lesson's claims are numbers and behaviors that tests can check: a deadline shrinks hop by hop, the retry budget stops retries after a fixed share, the breaker opens after enough failures, shedding refuses the excess. Most of these are tested in milliseconds with fake clocks, and the whole chain is tested in one process. A few tests run against the real containers.

## Where it appears

| Path | Kind of test |
| --- | --- |
| `libs/resilience/tests/unit/` | The policy objects with fake clocks (coverage gate: 95 percent of the whole library) |
| `libs/chassis/tests/unit/`, `services/*/tests/unit/` | Settings, tracing, the middleware, the hop client, the use cases with fakes |
| `services/stock/tests/integration/` | The SQL adapter against the real database (`make up` first) |
| `tests/system/` | The measuring tools, the policy page against the code, and the whole chain in one process |
| `tests/e2e/` | The chain across the running containers |
| `pyproject.toml` files | The configuration |

## How it works underneath

- pytest **collects** files named `test_*.py` and functions named `test_*`. Each function is one test.
- `assert a == b` is plain Python; pytest rewrites it so that on failure it prints both values.
- `with pytest.raises(SomeError):` passes only if the block raises that error.
- A **fixture** is a function marked `@pytest.fixture` that makes something a test needs, such as a database connection. A test asks for it by writing the fixture's name as an argument. Code before `yield` is set-up and code after it is clean-up. `scope="session"` creates it once for the whole run. Fixtures in a file called `conftest.py` are available to every test in that folder.
- `@pytest.mark.parametrize` runs one test function several times with different inputs.
- A **marker** is a label on a test. `-m "integration"` runs only tests with that label; `-m "not integration"` skips them. Markers must be listed in the configuration when `--strict-markers` is on.
- A **fake** is a simple stand-in object that a test passes instead of the real thing (a list instead of a database). A **unit test** checks one piece with fakes and is fast. An **integration test** uses real parts, such as a running database. An **end-to-end test** drives the whole system from the outside.
- Coverage works by watching which lines run. `--fail-under=95` makes the command fail when less than 95 percent is covered.

Most tests here are `async def` functions; `pytest-asyncio.md` explains how pytest runs them.

## Walkthrough of this repository's use

### Fake clocks make time controllable

```python
import pytest

from resilience.backoff import Backoff
from resilience.deadline import Deadline
from resilience.retry import AttemptFailed, RetriesExhausted, run_budgeted, run_naive
from resilience.retry_budget import RetryBudget


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def test_the_naive_loop_retries_immediately_three_times_with_the_same_timeout():
    seen = []

    async def attempt(timeout):
        seen.append(timeout)
        raise AttemptFailed("boom")

    with pytest.raises(RetriesExhausted) as caught:
        await run_naive(attempt)
    assert seen == [2.0, 2.0, 2.0]
    assert caught.value.attempts == 3


async def test_the_naive_loop_returns_the_first_success():
    results = iter([AttemptFailed("x"), "ok"])

    async def attempt(timeout):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    assert await run_naive(attempt) == "ok"

```

`Clock` is a tiny class with a `now` attribute and a `__call__` method, so an instance can be passed wherever the code expects a function that returns the time. The test sets `clock.now` itself, so a ten-second window costs no real time. The first test asserts the naive loop's exact behavior: three attempts, each with the same 2.0-second timeout, no pauses (`seen == [2.0, 2.0, 2.0]`). `pytest.raises(RetriesExhausted) as caught` checks the error and lets the test inspect it (`caught.value.attempts`).

### A helper that builds the scenario

```python
def budgeted(attempt, *, deadline_seconds=2.0, budget=None, clock=None, sleeps=None, **kwargs):
    clock = clock or Clock()
    sleeps = sleeps if sleeps is not None else []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    return run_budgeted(
        attempt,
        deadline=Deadline.after(clock(), deadline_seconds),
        budget=budget or RetryBudget(clock, ratio=0.1, min_retries=3),
        backoff=Backoff(base=0.05, cap=0.5),
        clock=clock,
        sleep=sleep,
        rand=lambda: 1.0,
        **kwargs,
    )
```

`budgeted(...)` is a helper, not a test (no `test_` prefix). It wires `run_budgeted` to a fake clock and a fake `sleep` that records each pause and moves the clock forward, and to `rand=lambda: 1.0`, the largest jitter, so the pauses are predictable. Each test then calls it with a different attempt function and asserts what happened.

### Fixtures

```python
import os

import pytest_asyncio

from chassis.postgres import create_pool


@pytest_asyncio.fixture
async def pool():
    password = os.environ["STOCK_APP_PASSWORD"]
    port = os.environ.get("STOCK_DB_PORT", "43432")
    pool = await create_pool(f"postgresql://stock_app:{password}@127.0.0.1:{port}/stock", name="it", wait_seconds=10)
    yield pool
    await pool.close()
```

A fixture in `conftest.py`: `pool` builds a pool as the application role, hands it to any test that names `pool` as an argument, and closes it after the `yield`. The password and port come from environment variables that `make` exports from `.env`. This one is an async fixture (see `pytest-asyncio.md`).

### The whole chain in one process

```python
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
```

`build_chain()` (in `tests/system/chain.py`) creates the four real FastAPI apps and wires each one's client to the next over in-memory transports. The test sends one request with `X-Trace-Force`, then reads the spans each service stored for that trace and asserts that the deadline in each hop is smaller than in the one before it: 2000, then less, then less. That is the lesson's "shrinking deadline" as a test.

### Coverage gates

```make
# Unit tests need no services. The domain and application of every service, and all of resilience, stay at 95% or more.
test: install ## unit tests with coverage for every package, and the design page against the code (no services needed)
	@for p in $(PACKAGES); do \
	  name=$$(basename $$p); \
	  (cd $$p && echo "== unit tests $$p" && uv run pytest tests/unit -m "not integration" --cov=$$name --cov-report=term-missing:skip-covered -q \
	    && if [ -d src/$$name/domain ]; then uv run coverage report --include="src/$$name/domain/*,src/$$name/application/*" --fail-under=95; fi \
	    && if [ $$name = resilience ]; then uv run coverage report --fail-under=95; fi) || exit 1; \
	done
	uv run pytest tests/system -q
```

For every package: unit tests with coverage, then `coverage report --fail-under=95` for `domain/` and `application/` when the package has them, and for the whole package when it is `resilience`. The root `tests/system` run follows.

### Configuration

The markers (`e2e`, `experiment`, `integration`) are registered in the `pyproject.toml` files and `--strict-markers` turns a typo into an error. `asyncio_mode = "auto"` is explained in `pytest-asyncio.md` (see `toml.md`).

## Every test file, one by one

How to read a test file, for a reader who has never seen one:

- A test is a function whose name starts with `test_`. pytest runs it. If it finishes without an error, it passes.
- `assert condition` stops the test with a failure if the condition is false. pytest shows both sides of a failed comparison.
- `with pytest.raises(SomeError):` expects the code inside the block to raise that error; if it does not, the test fails.
- A **fake** is a small stand-in object that has the same methods as a real part (a database, a mail provider) but keeps everything in memory.
- Names in `test_` functions are long on purpose: each one is a sentence that says what must be true.

### `libs/chassis/tests/unit/test_settings_and_logging.py`

```python
import io
import json
import logging

import pytest

from chassis.correlation import correlation_scope, current_correlation_id, sanitize_correlation_id
from chassis.errors import ConfigError
from chassis.health import HealthRegistry
from chassis.logging import JsonFormatter, configure_logging
from chassis.settings import EnvReader

```

Imports. `io` (in-memory streams), `json`, `logging`, `pytest` and the chassis names under test. There are no docstrings: ruff allows that in tests (see `ruff.md`).

```python
def test_env_reader_returns_defaults_and_typed_values():
    reader = EnvReader({"PORT": "8080", "RATE": "0.5", "MODE": "fast"})
    assert reader.integer("PORT", 1) == 8080
    assert reader.integer("MISSING", 7) == 7
    assert reader.number("RATE", 1.0) == 0.5
    assert reader.choice("MODE", "slow", ("slow", "fast")) == "fast"
    assert reader.text("NAME", "dflt") == "dflt"
    reader.finish()


def test_env_reader_reports_every_problem_at_once():
    reader = EnvReader({"PORT": "abc", "RATE": "-1", "MODE": "weird", "COUNT": "99"})
    reader.integer("PORT", 1)
    reader.number("RATE", 1.0)
    reader.choice("MODE", "slow", ("slow", "fast"))
    reader.integer("COUNT", 1, maximum=10)
    reader.text("DATABASE_URL")
    with pytest.raises(ConfigError) as caught:
        reader.finish()
    message = str(caught.value)
    for fragment in (
        "PORT must be an integer",
        "RATE must be between",
        "MODE must be one of",
        "COUNT must be between",
        "DATABASE_URL is required",
    ):
        assert fragment in message


def test_env_reader_rejects_a_non_number_float():
    reader = EnvReader({"RATE": "fast"})
    reader.number("RATE", 1.0)
    with pytest.raises(ConfigError):
        reader.finish()
```

`EnvReader` is given a plain dictionary instead of the real environment. The first test checks typed values and defaults (`reader.integer("MISSING", 7) == 7`) and that `finish()` passes silently. The second feeds five bad inputs (not a number, a negative rate, an unknown mode, a count above the maximum, a missing required variable); `finish()` inside `pytest.raises(ConfigError) as caught` must raise one error whose text (`str(caught.value)`) names every problem (the `for fragment in (...)` loop). The third checks a non-numeric float is rejected.

```python
def test_sanitize_correlation_id_keeps_safe_ids_and_replaces_unsafe_ones():
    assert sanitize_correlation_id("abc-123_x.y") == "abc-123_x.y"
    assert sanitize_correlation_id('bad"\nid') != 'bad"\nid'
    assert len(sanitize_correlation_id(None)) == 32


def test_correlation_scope_sets_and_restores_the_id():
    assert current_correlation_id() is None
    with correlation_scope("req-1") as value:
        assert value == "req-1"
        assert current_correlation_id() == "req-1"
    assert current_correlation_id() is None
```

A safe correlation id is kept, an unsafe one (a quote and a newline) is replaced, and `None` gives a 32-character id. `correlation_scope` sets the id inside the block and restores `None` after it.

```python
def test_json_logs_carry_the_correlation_id_and_mask_secrets():
    stream = io.StringIO()
    configure_logging("demo", "INFO", stream)
    with correlation_scope("req-9"):
        logging.getLogger("demo.test").info("hello", extra={"order_id": "o-1", "api_token": "abc"})
    line = json.loads(stream.getvalue())
    assert (line["service"], line["correlation_id"], line["order_id"], line["api_token"], line["level"]) == (
        "demo",
        "req-9",
        "o-1",
        "***",
        "INFO",
    )


def test_json_logs_include_exception_text():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("demo"))
    logger = logging.getLogger("demo.exc")
    logger.handlers[:] = [handler]
    logger.propagate = False
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("failed")
    assert "RuntimeError: boom" in json.loads(stream.getvalue())["exc"]


def test_health_registry_reports_failed_checks_by_name():
    registry = HealthRegistry()
    registry.add("ok", lambda: None)

    def broken():
        raise ConnectionError

    registry.add("broken", broken)
    report = registry.run()
    assert report.ready is False
    assert report.checks == {"ok": "ok", "broken": "failed: ConnectionError"}
```

A log line written inside a correlation scope into an in-memory stream carries the service, the correlation id, the extra field, the masked `api_token` (`"***"`) and the level; the assertion compares two tuples. The next test logs from an `except` block with `logger.exception` and finds `RuntimeError: boom` in the `exc` field. The last checks the health registry: a check that raises `ConnectionError` is reported as `"failed: ConnectionError"` and `report.ready is False`.

### `libs/chassis/tests/unit/test_tracing_observability_upstream.py`

```python
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

```

Imports: `asyncio`, `httpx`, `FastAPI` and `TestClient`, and the chassis, tracing and resilience names. `VALID` is a well-formed `traceparent` header.

```python
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

```

A valid header parses into its three parts and renders back to the same text; changing the flag to `-00` gives `sampled is False` (a `# type: ignore[union-attr]` comment silences one type-checker complaint). Five bad values (`None`, empty, garbage, an all-zero trace id, an upper-case version of the header) all give `None`. A child span keeps the trace id and the sampling decision and gets a new span id.

```python
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
```

`span(...)` builds a `Span` quickly. The store test uses `SpanStore(max_traces=2, max_spans_per_trace=2)`: after recording spans for traces `a`, `b`, `c` only the newest two traces remain (`["b", "c"]`), so trace `a` is gone. A second store with `max_spans_per_trace=2` drops the third span and returns the kept ones in start order.

```python
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
```

`make_app(sample_rate)` builds a small FastAPI app with the real middleware, the metrics route and the admin router, a route `/work` that records the current deadline and span in a dictionary, and a route `/boom` that raises. `TestClient(app, raise_server_exceptions=False)` returns a 500 instead of raising in the test. `rand=lambda: 0.0` makes the sampling decision deterministic.

```python
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
```

The middleware joins the caller's trace (the `x-trace-id` header equals the trace id; the stored span has the caller's span as parent), reads the deadline (`1.0 < remaining <= 1.5`), and counts the request: the `/metrics` text contains `calls_received_total{route="/work"} 1.0` and `in_flight 0.0`. A request with no trace starts one, stored only when sampled or forced (`X-Trace-Force`). An error is a 500 and a malformed deadline (`"soon"`) is ignored. The admin routes switch the profile, reject `chaos` with 422, list traces and return a stored trace.

```python
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
```

`hop(handler, deadline_clock)` builds a `HopClient` over `httpx.MockTransport` (a fake server). The async tests: a successful call sends a `traceparent` starting with `00-` and `x-deadline-ms: 2000` and counts `calls_sent_total{target="down"} 1.0`; for statuses 500, 503 and 429 the failure is retryable and for 404 it is not (`for status, retryable in (...)` with `lambda request, s=status: httpx.Response(s)` binding the loop value as a default argument); a slow handler (`await asyncio.sleep(1)`) with a 0.05 s timeout gives `AttemptFailed("timeout")` and a recorded span with status `timeout` and attribute `attempt == 2`; a refused connection gives `AttemptFailed` matching `ConnectError`; a 503 with `Retry-After: 2` sets `retry_after == 2.0`.

### `libs/resilience/tests/unit/test_policy_objects.py`

```python
import pytest

from resilience.backoff import Backoff
from resilience.breaker import BreakerState, CircuitBreaker
from resilience.deadline import Deadline
from resilience.limiter import ConcurrencyLimiter
from resilience.retry_budget import RetryBudget


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

```

`Clock` is a callable object with a settable `now` and `advance(seconds)`: a fake clock.

```python
def test_a_deadline_counts_down_and_never_goes_negative():
    deadline = Deadline.after(100.0, 2.0)
    assert deadline.remaining(100.5) == pytest.approx(1.5)
    assert deadline.expired(102.0)
    assert deadline.remaining(105.0) == 0.0
    assert deadline.header_value(100.5) == "1500"


def test_the_attempt_timeout_is_the_hop_limit_or_what_is_left_minus_the_margin():
    deadline = Deadline.after(0.0, 2.0)
    assert deadline.attempt_timeout(0.0, hop_limit=1.2, margin=0.05) == pytest.approx(1.2)
    assert deadline.attempt_timeout(1.5, hop_limit=1.2, margin=0.05) == pytest.approx(0.45)
    assert deadline.attempt_timeout(1.99, hop_limit=1.2, margin=0.05) == 0.0


@pytest.mark.parametrize(("value", "expected"), [("1500", 1.5), ("0", 0.0), ("999999999", 60.0), ("12.5", 0.0125)])
def test_a_deadline_header_is_parsed_and_clamped(value, expected):
    deadline = Deadline.from_header(value, now=10.0)
    assert deadline is not None
    assert deadline.remaining(10.0) == pytest.approx(expected)


@pytest.mark.parametrize("value", [None, "", "abc", "-5", "nan"])
def test_a_missing_or_malformed_deadline_header_is_ignored(value):
    assert Deadline.from_header(value, now=10.0) is None

```

Deadline: `remaining(100.5)` is 1.5 (`pytest.approx` compares floats with tolerance), it expires at 102.0, never goes negative, and `header_value` is `"1500"`. The attempt timeout is the hop limit, or what is left minus the margin (0.45 at 1.5 s), or 0.0 when nothing useful is left. Parametrized header parsing: `"1500"` gives 1.5 s, `"999999999"` is clamped to 60 s; `None`, empty, `abc`, `-5` and `nan` give no deadline.

```python
def test_backoff_grows_exponentially_is_capped_and_is_scaled_by_the_random_draw():
    backoff = Backoff(base=0.05, cap=0.3, multiplier=2.0)
    assert [backoff.delay(n, 1.0) for n in (1, 2, 3, 4, 5)] == [0.05, 0.1, 0.2, 0.3, 0.3]
    assert backoff.delay(3, 0.0) == 0.0
    assert backoff.delay(3, 0.5) == pytest.approx(0.1)


def test_the_retry_budget_allows_ten_percent_of_the_requests_and_a_small_minimum():
    clock = Clock()
    budget = RetryBudget(clock, ratio=0.1, window_seconds=10, min_retries=3)
    assert [budget.try_acquire_retry() for _ in range(4)] == [True, True, True, False]
    for _ in range(100):
        budget.record_request()
    granted = sum(budget.try_acquire_retry() for _ in range(50))
    assert granted == 10 - 3
    assert budget.retries_in_window == 10
    assert budget.requests_in_window == 100


def test_the_retry_budget_forgets_old_events():
    clock = Clock()
    budget = RetryBudget(clock, ratio=0.1, window_seconds=10, min_retries=1)
    assert budget.try_acquire_retry() is True
    assert budget.try_acquire_retry() is False
    clock.advance(10.1)
    assert budget.try_acquire_retry() is True
```

Backoff doubles and is capped (`[0.05, 0.1, 0.2, 0.3, 0.3]`), and a random draw of 0.0 gives 0 while 0.5 gives half. The budget allows the minimum (three) then refuses; after 100 requests it grants `10 - 3` more (ten percent of 100 is ten in total, three already spent); it forgets events older than the window (`clock.advance(10.1)`).

```python
def state_of(breaker: CircuitBreaker) -> BreakerState:
    return breaker.state


def make_breaker(clock):
    return CircuitBreaker(clock, failure_ratio=0.5, min_calls=10, window_seconds=10, open_seconds=5, half_open_probes=3)


def fail(breaker, n):
    for _ in range(n):
        assert breaker.allow()
        breaker.record_failure()


def test_the_breaker_stays_closed_below_the_minimum_number_of_calls_and_opens_at_the_failure_ratio():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 9)
    assert state_of(breaker) is BreakerState.CLOSED
    fail(breaker, 1)
    assert state_of(breaker) is BreakerState.OPEN
    assert breaker.allow() is False


def test_the_breaker_does_not_open_when_successes_keep_the_ratio_low():
    breaker = make_breaker(Clock())
    for _ in range(20):
        breaker.record_success()
        breaker.record_success()
        breaker.record_failure()
    assert state_of(breaker) is BreakerState.CLOSED


def test_after_the_cool_down_a_few_probes_are_allowed_and_success_closes_the_breaker():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 10)
    clock.advance(4.9)
    assert breaker.allow() is False
    clock.advance(0.2)
    assert state_of(breaker) is BreakerState.HALF_OPEN
    assert [breaker.allow() for _ in range(4)] == [True, True, True, False]
    for _ in range(3):
        breaker.record_success()
    assert state_of(breaker) is BreakerState.CLOSED
    assert breaker.allow() is True


def test_a_failed_probe_opens_the_breaker_again_for_a_full_cool_down():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 10)
    clock.advance(5.1)
    assert breaker.allow() is True
    breaker.record_failure()
    assert state_of(breaker) is BreakerState.OPEN
    clock.advance(4.0)
    assert breaker.allow() is False


def test_old_failures_age_out_of_the_window():
    clock = Clock()
    breaker = make_breaker(clock)
    fail(breaker, 9)
    clock.advance(11)
    breaker.record_failure()
    assert state_of(breaker) is BreakerState.CLOSED

```

Circuit breaker tests with helpers `make_breaker` and `fail(breaker, n)`: it stays closed at nine failures and opens at the tenth; it does not open when successes keep the ratio low (two successes per failure, twenty times); after the rest period (4.9 s refused, 5.1 s half-open) three probes are allowed and the fourth is refused, and three successes close it; a failed probe opens it again for a full rest period; old failures age out of the window (eleven seconds later one failure is not enough).

```python
def test_the_limiter_sheds_beyond_its_limit_and_frees_slots_on_release():
    limiter = ConcurrencyLimiter(2)
    assert [limiter.try_acquire() for _ in range(3)] == [True, True, False]
    assert limiter.in_flight == 2
    limiter.release()
    assert limiter.try_acquire() is True
    limiter.release()
    limiter.release()
    limiter.release()
    assert limiter.in_flight == 0
    assert limiter.limit == 2


def test_a_limiter_needs_a_positive_limit():
    with pytest.raises(ValueError, match="at least 1"):
        ConcurrencyLimiter(0)
```

The limiter refuses the third request of two slots and frees slots on release (extra releases never go below zero); a limit of 0 raises `ValueError` matching `at least 1`.

### `libs/resilience/tests/unit/test_retry_loops.py`

```python
import pytest

from resilience.backoff import Backoff
from resilience.deadline import Deadline
from resilience.retry import AttemptFailed, RetriesExhausted, run_budgeted, run_naive
from resilience.retry_budget import RetryBudget


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def test_the_naive_loop_retries_immediately_three_times_with_the_same_timeout():
    seen = []

    async def attempt(timeout):
        seen.append(timeout)
        raise AttemptFailed("boom")

    with pytest.raises(RetriesExhausted) as caught:
        await run_naive(attempt)
    assert seen == [2.0, 2.0, 2.0]
    assert caught.value.attempts == 3


async def test_the_naive_loop_returns_the_first_success():
    results = iter([AttemptFailed("x"), "ok"])

    async def attempt(timeout):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    assert await run_naive(attempt) == "ok"


def budgeted(attempt, *, deadline_seconds=2.0, budget=None, clock=None, sleeps=None, **kwargs):
    clock = clock or Clock()
    sleeps = sleeps if sleeps is not None else []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    return run_budgeted(
        attempt,
        deadline=Deadline.after(clock(), deadline_seconds),
        budget=budget or RetryBudget(clock, ratio=0.1, min_retries=3),
        backoff=Backoff(base=0.05, cap=0.5),
        clock=clock,
        sleep=sleep,
        rand=lambda: 1.0,
        **kwargs,
    )

```

The naive loop tries three times with the same 2.0 timeout and reports `RetriesExhausted` with `attempts == 3`; it returns the first success. `budgeted(...)` is a helper that wires `run_budgeted` to a fake clock, a fake `sleep` that records pauses and moves the clock, and `rand=lambda: 1.0` (maximum jitter, so pauses are predictable); `**kwargs` passes extra named arguments through.

```python
async def test_a_success_needs_no_retry_and_counts_as_one_request():
    clock = Clock()
    budget = RetryBudget(clock)

    async def attempt(timeout):
        return "ok"

    assert await budgeted(attempt, budget=budget, clock=clock) == "ok"
    assert budget.requests_in_window == 1
    assert budget.retries_in_window == 0


async def test_a_retryable_error_is_retried_with_a_backoff_pause_and_succeeds():
    calls = []
    sleeps: list[float] = []

    async def attempt(timeout):
        calls.append(timeout)
        if len(calls) < 3:
            raise AttemptFailed("500")
        return "ok"

    assert await budgeted(attempt, sleeps=sleeps) == "ok"
    assert len(calls) == 3
    assert sleeps == [0.05, 0.1]


async def test_each_attempt_gets_no_more_time_than_is_left_of_the_deadline():
    clock = Clock()
    seen = []

    async def attempt(timeout):
        seen.append(round(timeout, 3))
        clock.now += timeout
        raise AttemptFailed("slow")

    with pytest.raises(RetriesExhausted):
        await budgeted(attempt, clock=clock, deadline_seconds=2.0, hop_timeout=1.8)
    assert seen[0] == 1.8
    assert sum(seen) <= 2.0


async def test_a_non_retryable_error_stops_at_once():
    calls = []

    async def attempt(timeout):
        calls.append(1)
        raise AttemptFailed("400", retryable=False)

    with pytest.raises(RetriesExhausted, match="not retryable"):
        await budgeted(attempt)
    assert len(calls) == 1
```

A success needs no retry and counts one request. A retryable error is retried twice with pauses `[0.05, 0.1]`. Each attempt gets no more time than is left of the deadline (the attempt function advances the fake clock by its timeout, and the sum stays at most 2.0). A non-retryable error stops at once (`match="not retryable"`).

```python
async def test_when_the_budget_is_spent_the_next_failure_is_not_retried():
    clock = Clock()
    budget = RetryBudget(clock, ratio=0.0, min_retries=1)
    calls = []

    async def attempt(timeout):
        calls.append(1)
        raise AttemptFailed("500")

    with pytest.raises(RetriesExhausted, match="budget"):
        await budgeted(attempt, budget=budget, clock=clock)
    assert len(calls) == 2
    with pytest.raises(RetriesExhausted, match="budget"):
        await budgeted(attempt, budget=budget, clock=clock)
    assert len(calls) == 3


async def test_a_server_retry_after_longer_than_the_time_left_ends_the_retries():
    async def attempt(timeout):
        raise AttemptFailed("503", retry_after=5.0)

    with pytest.raises(RetriesExhausted, match="not enough time"):
        await budgeted(attempt)


async def test_a_server_retry_after_that_fits_is_honored():
    sleeps: list[float] = []
    calls = []

    async def attempt(timeout):
        calls.append(1)
        if len(calls) == 1:
            raise AttemptFailed("503", retry_after=0.4)
        return "ok"

    assert await budgeted(attempt, sleeps=sleeps) == "ok"
    assert sleeps == [0.4]


async def test_an_expired_deadline_refuses_to_try_at_all():
    calls = []

    async def attempt(timeout):
        calls.append(1)

    with pytest.raises(RetriesExhausted, match="deadline too short"):
        await budgeted(attempt, deadline_seconds=0.05)
    assert calls == []


async def test_on_retry_is_told_about_each_retry():
    reasons = []

    async def attempt(timeout):
        raise AttemptFailed("boom")

    with pytest.raises(RetriesExhausted):
        await budgeted(attempt, on_retry=lambda n, why: reasons.append((n, why)))
    assert reasons == [(1, "boom"), (2, "boom")]
```

With a spent budget (ratio 0, minimum 1) one retry is allowed and then refused, and a second call gets no retry at all; a `Retry-After` of 5 s is longer than the time left so retries end (`match="not enough time"`), one of 0.4 s is honored (`sleeps == [0.4]`); a deadline of 0.05 s refuses to try at all (`calls == []`, `match="deadline too short"`); `on_retry` is told `[(1, "boom"), (2, "boom")]`.

```python
def test_profile_holder_switches_profiles_and_errors_carry_their_hints():
    from resilience.errors import DeadlineExceeded, Overloaded
    from resilience.profile import Profile, ProfileHolder

    holder = ProfileHolder()
    assert holder.get() is Profile.NAIVE
    holder.set(Profile.FULL)
    assert holder.get() is Profile.FULL
    assert Overloaded(2.0).retry_after == 2.0
    assert isinstance(DeadlineExceeded(), Exception)
    assert [p.value for p in Profile] == ["naive", "deadline-retry", "full"]
```

The profile holder switches profiles; `Overloaded(2.0).retry_after == 2.0`; the three profile values are `naive`, `deadline-retry`, `full`.

### The gateway tests

```python
import pytest

from gateway.application.handle_search import HandleSearch, Unavailable
from gateway.domain.models import SearchAnswer
from resilience.backoff import Backoff
from resilience.profile import Profile
from resilience.retry import AttemptFailed
from resilience.retry_budget import RetryBudget


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Search:
    def __init__(self, *results, clock=None, spend=0.0):
        self.results = list(results)
        self.calls = []
        self.clock = clock
        self.spend = spend

    async def search(self, query, timeout, deadline, attempt):
        self.calls.append((timeout, deadline is not None, attempt))
        if self.clock:
            self.clock.now += self.spend
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class Telemetry:
    def __init__(self):
        self.retries = []
        self.outcomes = []

    def retry(self, reason):
        self.retries.append(reason)

    def outcome(self, outcome):
        self.outcomes.append(outcome)


def answer(degraded=False):
    return SearchAnswer("widget", "sku-001", "9.99", True, degraded, False)


def make(search, profile=Profile.DEADLINE_RETRY, clock=None, min_retries=3):
    clock = clock or Clock()
    telemetry = Telemetry()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    handle = HandleSearch(
        search,
        lambda: profile,
        RetryBudget(clock, ratio=0.1, min_retries=min_retries),
        Backoff(0.05, 0.5),
        clock,
        telemetry,
        sleep,
        lambda: 1.0,
    )
    return handle, telemetry, sleeps, clock

```

`Clock`, `Search` and `Telemetry` are fakes. `Search(*results, clock=None, spend=0.0)` returns the given results in turn (the last repeats) or raises them if they are exceptions (`isinstance(result, Exception)`), recording each call as `(timeout, deadline is not None, attempt)`. `make(...)` builds `HandleSearch` with a fake sleep that moves the clock and `lambda: 1.0` for randomness.

```python
async def test_a_good_answer_passes_through_with_a_deadline_header_and_no_retry():
    search = Search(answer())
    handle, telemetry, _, _ = make(search)
    assert (await handle("widget")).price == "9.99"
    assert search.calls == [(1.8, True, 1)]
    assert telemetry.outcomes == ["ok"]


async def test_a_degraded_answer_is_reported_as_degraded_not_as_failure():
    handle, telemetry, _, _ = make(Search(answer(degraded=True)))
    assert (await handle("widget")).degraded is True
    assert telemetry.outcomes == ["degraded"]


async def test_a_failure_is_retried_with_backoff_and_counted():
    search = Search(AttemptFailed("status 503"), answer())
    handle, telemetry, sleeps, _ = make(search)
    assert (await handle("widget")).price == "9.99"
    assert sleeps == [0.05]
    assert telemetry.retries == ["status 503"]
    assert len(search.calls) == 2


async def test_when_retries_run_out_the_client_is_told_to_come_back_later():
    handle, telemetry, _, _ = make(Search(AttemptFailed("status 500")))
    with pytest.raises(Unavailable) as caught:
        await handle("widget")
    assert caught.value.retry_after == 1.0
    assert telemetry.outcomes == ["unavailable"]


async def test_the_retry_budget_caps_retries_when_everything_fails():
    search = Search(AttemptFailed("status 500"))
    handle, telemetry, _, _ = make(search, min_retries=1)
    for _ in range(20):
        with pytest.raises(Unavailable):
            await handle("widget")
    assert len(telemetry.retries) <= 3
    assert len(search.calls) <= 20 + 3


async def test_the_naive_profile_has_no_deadline_no_backoff_and_three_immediate_attempts():
    search = Search(AttemptFailed("status 500"))
    handle, telemetry, sleeps, _ = make(search, Profile.NAIVE)
    with pytest.raises(Unavailable):
        await handle("widget")
    assert search.calls == [(2.0, False, 1), (2.0, False, 2), (2.0, False, 3)]
    assert sleeps == []
    assert telemetry.retries == []
```

A good answer passes with one call (timeout 1.8, with a deadline); a degraded answer is reported as degraded, not as a failure; a failure is retried after a 0.05 pause and counted; when retries run out the client gets `Unavailable` with `retry_after == 1.0`; with a small budget, twenty failing requests cause at most three retries; the naive profile makes three immediate attempts of 2.0 s with no deadline and no pause.

```python
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

```

A fake `Search` records the deadline it received. `client(search, sample_rate)` builds the real gateway app with `no_sleep` and fixed randomness.

```python
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
```

The search returns the price and a 32-character `x-trace-id` header; a chain that cannot answer gives 503 with `retry-after: 1`; a forced trace is stored with the span `gateway /search` and readable through the admin route; an unsampled one is not stored unless the sample rate is 1.0; settings defaults and validation (`TRACE_SAMPLE_RATE=3` is rejected).

### The search tests

```python
import pytest

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded, Overloaded
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import Profile
from resilience.retry import AttemptFailed
from search.application.search_products import PricingUnavailable, SearchProducts
from search.domain.models import CATALOG_SIZE, SearchResult, resolve_sku


class Pricing:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def quote(self, query, sku, timeout, deadline, attempt):
        self.calls.append((sku, timeout, deadline is not None, attempt))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class Telemetry:
    def __init__(self):
        self.shed_count = 0
        self.degraded_count = 0

    def shed(self):
        self.shed_count += 1

    def degraded(self):
        self.degraded_count += 1


def make(pricing, profile=Profile.FULL, limit=2):
    telemetry = Telemetry()
    return SearchProducts(pricing, ConcurrencyLimiter(limit), lambda: profile, lambda: 100.0, telemetry), telemetry


def ok(query="widget"):
    return SearchResult(query, resolve_sku(query), "9.99", True, False, False)


def test_a_query_resolves_to_the_same_catalog_sku_every_time_regardless_of_case_and_spacing():
    assert resolve_sku("Widget  Large") == resolve_sku("widget large")
    assert resolve_sku("widget") != resolve_sku("gadget") or CATALOG_SIZE == 1
    assert resolve_sku("anything").startswith("sku-")
    assert 1 <= int(resolve_sku("anything")[4:]) <= CATALOG_SIZE

```

Fakes for pricing and telemetry; `make` builds `SearchProducts` with a limiter and a constant clock; `ok()` builds a result. The first test checks that `resolve_sku` ignores case and extra spaces, always returns `sku-` followed by a number between 1 and 20.

```python
async def test_the_priced_result_is_returned_with_one_attempt_inside_the_deadline():
    pricing = Pricing(ok())
    search, _ = make(pricing)
    result = await search("widget", Deadline.after(100.0, 2.0))
    assert result.price == "9.99"
    assert pricing.calls == [(resolve_sku("widget"), 1.2, True, 1)]


async def test_when_pricing_fails_a_partial_degraded_result_is_returned_instead_of_an_error():
    search, telemetry = make(Pricing(AttemptFailed("timeout")), Profile.DEADLINE_RETRY)
    result = await search("widget", None)
    assert (result.price, result.degraded) == (None, True)
    assert telemetry.degraded_count == 1


async def test_a_deadline_too_short_to_try_is_refused():
    pricing = Pricing(ok())
    search, _ = make(pricing)
    with pytest.raises(DeadlineExceeded):
        await search("widget", Deadline.after(100.0, 0.1))
    assert pricing.calls == []


async def test_beyond_the_concurrency_limit_the_full_profile_sheds_with_a_retry_hint():
    import asyncio

    release = asyncio.Event()

    class Slow:
        async def quote(self, query, sku, timeout, deadline, attempt):
            await release.wait()
            return ok(query)

    search, telemetry = make(Slow(), limit=2)
    running = [asyncio.create_task(search("a", None)) for _ in range(2)]
    await asyncio.sleep(0)
    with pytest.raises(Overloaded) as caught:
        await search("b", None)
    assert caught.value.retry_after == 1.0
    assert telemetry.shed_count == 1
    release.set()
    await asyncio.gather(*running)
    assert (await search("c", None)).degraded is False


async def test_the_other_profiles_never_shed():
    pricing = Pricing(ok())
    search, telemetry = make(pricing, Profile.DEADLINE_RETRY, limit=1)
    results = [await search("widget", None) for _ in range(3)]
    assert len(results) == 3
    assert telemetry.shed_count == 0


async def test_the_naive_profile_tries_three_times_with_two_second_timeouts_and_then_fails_upward():
    pricing = Pricing(AttemptFailed("status 500"))
    search, _ = make(pricing, Profile.NAIVE)
    with pytest.raises(PricingUnavailable):
        await search("widget", Deadline.after(100.0, 2.0))
    assert [(t, sent, n) for _, t, sent, n in pricing.calls] == [(2.0, False, 1), (2.0, False, 2), (2.0, False, 3)]
```

One attempt inside the deadline (timeout 1.2); a failure gives a degraded result with no price and counts it; a deadline of 0.1 s is refused without calling pricing; shedding: two slow requests fill a limit of 2 (`asyncio.Event` is a flag one task sets for another to wait on; `asyncio.create_task` starts them; `await asyncio.sleep(0)` lets them start), the third raises `Overloaded` with `retry_after == 1.0`, and after `release.set()` the first two finish and a new request works; the other profiles never shed; the naive profile tries three times at 2.0 s with no deadline and fails upward with `PricingUnavailable`.

```python
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
```

API tests: the price and sku are returned; a failing pricing is a 502 in the naive profile and a partial 200 with `degraded_total 1.0` in the metrics after the profile is switched through `/admin/profile`; a shed request is 503 with `retry-after: 1` and `shed_total 1.0` (the test takes the only slot itself with `limiter.try_acquire()`); an expired deadline header (`X-Deadline-Ms: 100`) is 504 and a blank query 422; settings defaults (`concurrency_limit == 40`) and validation.

### The pricing tests

```python
from decimal import Decimal

import pytest

from pricing.application.quote_price import QuotePrice, StockUnavailable
from pricing.domain.models import Quote, base_price
from pricing.infrastructure.cache import InMemoryQuoteCache
from resilience.breaker import BreakerState, CircuitBreaker
from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from resilience.profile import Profile
from resilience.retry import AttemptFailed


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Stock:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def check(self, sku, timeout, deadline, attempt):
        self.calls.append((timeout, deadline is not None, attempt))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class Telemetry:
    def __init__(self):
        self.fallbacks = []
        self.rejected = 0
        self.states = []

    def fallback(self, reason):
        self.fallbacks.append(reason)

    def breaker_rejected(self):
        self.rejected += 1

    def breaker_state(self, state):
        self.states.append(state)


def make(stock, profile=Profile.FULL):
    clock = Clock()
    telemetry = Telemetry()
    cache = InMemoryQuoteCache(clock)
    breaker = CircuitBreaker(
        clock, failure_ratio=0.5, min_calls=4, window_seconds=10, open_seconds=5, half_open_probes=2
    )
    return QuotePrice(stock, cache, breaker, lambda: profile, clock, telemetry), clock, cache, breaker, telemetry

```

Fakes `Clock`, `Stock` and `Telemetry`; `make(stock, profile)` builds `QuotePrice` with a cache and a small breaker (`min_calls=4`, `open_seconds=5`, `half_open_probes=2`).

```python
async def test_a_stock_reading_becomes_a_fresh_quote_and_is_cached():
    use_case, clock, cache, _, _ = make(Stock(5))
    quote = await use_case("sku-001", Deadline.after(clock(), 2.0))
    assert (quote.in_stock, quote.degraded, quote.stale) == (True, False, False)
    assert cache.get("sku-001") == quote


async def test_low_stock_raises_the_price_by_fifteen_percent():
    use_case, _, _, _, _ = make(Stock(3))
    quote = await use_case("sku-001", None)
    assert quote.price == (base_price("sku-001") * Decimal("1.15")).quantize(Decimal("0.01"))


async def test_one_attempt_gets_the_hop_limit_or_what_is_left_of_the_deadline():
    stock = Stock(5)
    use_case, clock, _, _, _ = make(stock)
    await use_case("sku-001", Deadline.after(clock(), 2.0))
    clock.now += 1.5
    await use_case("sku-001", Deadline.after(clock() - 1.5, 2.0))
    assert [round(t, 2) for t, _, _ in stock.calls] == [0.7, 0.45]
    assert all(sent for _, sent, _ in stock.calls)


async def test_a_deadline_too_short_to_try_is_refused_without_calling_stock():
    stock = Stock(5)
    use_case, clock, _, _, _ = make(stock)
    with pytest.raises(DeadlineExceeded):
        await use_case("sku-001", Deadline.after(clock(), 0.1))
    assert stock.calls == []


async def test_a_failure_returns_the_last_known_quote_flagged_stale():
    stock = Stock(5, AttemptFailed("timeout"))
    use_case, _, _, _, telemetry = make(stock, Profile.DEADLINE_RETRY)
    fresh = await use_case("sku-001", None)
    degraded = await use_case("sku-001", None)
    assert (degraded.price, degraded.degraded, degraded.stale) == (fresh.price, True, True)
    assert telemetry.fallbacks == ["timeout"]


async def test_with_nothing_cached_the_base_price_is_returned_flagged_stale():
    use_case, _, _, _, _ = make(Stock(AttemptFailed("status 500")), Profile.DEADLINE_RETRY)
    quote = await use_case("sku-009", None)
    assert (quote.price, quote.in_stock, quote.degraded, quote.stale) == (base_price("sku-009"), None, True, True)

```

A reading becomes a fresh quote and is cached; low stock raises the price by 15 percent (`Decimal("1.15")`, `quantize`); one attempt gets the hop limit 0.7 or what is left of the deadline (0.45); a deadline of 0.1 s is refused without calling stock; a failure returns the last known quote flagged stale; with nothing cached the base price is returned, stale and with `in_stock` unknown (`None`).

```python
async def test_the_breaker_opens_after_repeated_failures_and_then_stock_is_not_called():
    stock = Stock(AttemptFailed("timeout"))
    use_case, _, _, breaker, telemetry = make(stock)
    for _ in range(4):
        await use_case("sku-001", None)
    assert breaker.state is BreakerState.OPEN
    calls_before = len(stock.calls)
    quote = await use_case("sku-001", None)
    assert len(stock.calls) == calls_before
    assert quote.degraded
    assert telemetry.rejected == 1
    assert telemetry.fallbacks[-1] == "breaker open"


async def test_after_the_cool_down_probes_that_succeed_close_the_breaker_again():
    stock = Stock(
        AttemptFailed("timeout"), AttemptFailed("timeout"), AttemptFailed("timeout"), AttemptFailed("timeout"), 5
    )
    use_case, clock, _, breaker, telemetry = make(stock)
    for _ in range(4):
        await use_case("sku-001", None)
    clock.now += 5.1
    await use_case("sku-001", None)
    await use_case("sku-001", None)
    assert breaker.state is BreakerState.CLOSED
    assert telemetry.states[-1] is BreakerState.CLOSED


async def test_the_deadline_retry_profile_has_no_breaker_and_keeps_calling_stock():
    stock = Stock(AttemptFailed("timeout"))
    use_case, _, _, breaker, _ = make(stock, Profile.DEADLINE_RETRY)
    for _ in range(8):
        await use_case("sku-001", None)
    assert len(stock.calls) == 8
    assert breaker.state is BreakerState.CLOSED


async def test_the_naive_profile_tries_three_times_without_a_deadline_and_then_fails_upward():
    stock = Stock(AttemptFailed("status 500"))
    use_case, _, _, _, _ = make(stock, Profile.NAIVE)
    with pytest.raises(StockUnavailable):
        await use_case("sku-001", Deadline.after(100.0, 2.0))
    assert [(t, sent, n) for t, sent, n in stock.calls] == [(2.0, False, 1), (2.0, False, 2), (2.0, False, 3)]


async def test_the_naive_profile_returns_a_fresh_quote_when_a_retry_succeeds():
    stock = Stock(AttemptFailed("status 500"), 5)
    use_case, _, _, _, _ = make(stock, Profile.NAIVE)
    assert (await use_case("sku-001", None)).degraded is False
    assert len(stock.calls) == 2


def test_the_cache_expires_entries_and_evicts_the_oldest_when_full():
    clock = Clock()
    cache = InMemoryQuoteCache(clock, ttl_seconds=10, max_entries=2)
    for sku in ("a", "b", "c"):
        cache.put(Quote(sku, Decimal("1"), True))
    assert cache.get("a") is None
    assert cache.get("c") is not None
    clock.now += 11
    assert cache.get("c") is None
```

The breaker opens after four failures and the fifth call does not reach stock (`telemetry.rejected == 1`, last fallback `breaker open`); after the rest period (`clock.now += 5.1`) two successful probes close it; the `deadline-retry` profile has no breaker (eight calls reach stock, state stays closed); the naive profile makes three immediate 2.0 s attempts and fails with `StockUnavailable`, or succeeds when a retry works; the cache expires entries after the time to live and evicts the oldest when full.

```python
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
```

`client(stock)` builds the real app (the unused `AttemptFailed` import carries `# noqa: F401`, a ruff comment that silences one rule on that line). Tests: the route returns the quote; a dead stock store is a 502 in the naive profile; in the full profile it is a degraded stale 200 with `fallbacks_total{reason="timeout"} 1.0`; a 100 ms deadline is 504, an unknown profile 422 and the profile stays unchanged; settings.

### The stock tests

```python
import asyncio

import pytest

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults, InjectedFailure, InvalidFaults, UnknownSku


class Repo:
    def __init__(self, levels):
        self.levels = levels

    async def quantity(self, sku):
        return self.levels.get(sku)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(levels=None, faults=None, capacity=4, rand=0.5, sleep=None):
    faults = faults or Faults(0, 0.0)
    clock = Clock()

    async def default_sleep(seconds):
        await asyncio.sleep(0)

    use_case = CheckStock(
        Repo(levels or {"sku-001": 5}),
        lambda: faults,
        asyncio.Semaphore(capacity),
        clock=clock,
        sleep=sleep or default_sleep,
        rand=lambda: rand,
    )
    return use_case, clock

```

`Repo` returns quantities from a dictionary; `make(...)` builds `CheckStock` with a semaphore of the given capacity, a fixed random value and an optional `sleep` replacement.

```python
async def test_a_known_sku_returns_its_quantity_and_whether_it_is_in_stock():
    use_case, _ = make({"a": 5, "b": 0})
    assert (await use_case("a", None)).in_stock is True
    assert (await use_case("b", None)).in_stock is False
    assert (await use_case("a", None)).quantity == 5


async def test_an_unknown_sku_is_reported():
    use_case, _ = make()
    with pytest.raises(UnknownSku):
        await use_case("ghost", None)


async def test_the_fail_rate_decides_which_calls_fail():
    failing, _ = make(faults=Faults(0, 0.3), rand=0.29)
    passing, _ = make(faults=Faults(0, 0.3), rand=0.31)
    with pytest.raises(InjectedFailure):
        await failing("sku-001", None)
    assert (await passing("sku-001", None)).quantity == 5


async def test_the_configured_latency_is_waited():
    waited = []

    async def sleep(seconds):
        waited.append(seconds)

    use_case, _ = make(faults=Faults(1500, 0.0), sleep=sleep)
    await use_case("sku-001", None)
    assert waited == [1.5]


async def test_a_request_with_a_deadline_gives_up_when_the_deadline_passes_during_the_work():
    use_case, clock = make(faults=Faults(5000, 0.0), sleep=asyncio.sleep)
    with pytest.raises(DeadlineExceeded):
        await use_case("sku-001", Deadline.after(clock(), 0.05))


async def test_a_request_with_an_expired_deadline_is_refused_before_any_work():
    use_case, clock = make()
    with pytest.raises(DeadlineExceeded):
        await use_case("sku-001", Deadline.after(clock(), 0.0))


async def test_a_request_with_a_deadline_stops_waiting_for_capacity_when_the_deadline_passes():
    gate = asyncio.Semaphore(1)
    clock = Clock()
    use_case = CheckStock(Repo({"s": 1}), lambda: Faults(0, 0.0), gate, clock=clock, rand=lambda: 0.5)
    await gate.acquire()
    with pytest.raises(DeadlineExceeded):
        await use_case("s", Deadline.after(clock(), 0.05))
    gate.release()


async def test_a_request_without_a_deadline_waits_for_capacity_as_long_as_it_takes():
    gate = asyncio.Semaphore(1)
    use_case = CheckStock(Repo({"s": 1}), lambda: Faults(0, 0.0), gate, clock=Clock(), rand=lambda: 0.5)
    await gate.acquire()
    task = asyncio.create_task(use_case("s", None))
    await asyncio.sleep(0.05)
    assert not task.done()
    gate.release()
    assert (await task).quantity == 1
```

A known sku returns its quantity and in-stock flag; an unknown one raises `UnknownSku`; with `fail_rate` 0.3 a random draw of 0.29 fails and 0.31 passes; the configured latency is waited (`[1.5]`); with a deadline the work gives up when the deadline passes during the work (`sleep=asyncio.sleep` and a 5-second latency against a 0.05 s deadline); an expired deadline is refused before any work; a request with a deadline stops waiting for capacity (the test holds the only slot with `await gate.acquire()`); a request without a deadline waits as long as it takes (`task.done()` is false while the slot is held, and the result arrives after `gate.release()`).

```python
@pytest.mark.parametrize("latency", [-1, 60_001])
def test_faults_reject_an_out_of_range_latency(latency):
    with pytest.raises(InvalidFaults):
        Faults(latency, 0.0)


@pytest.mark.parametrize("rate", [-0.1, 1.1])
def test_faults_reject_an_out_of_range_fail_rate(rate):
    with pytest.raises(InvalidFaults):
        Faults(0, rate)
```

Parametrized range tests of `Faults`: latency -1 and 60001, fail rates -0.1 and 1.1 are invalid.

```python
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
```

API tests with a fake repository: a known sku returns `{"sku", "inStock", "quantity"}`; an unknown sku is 404 and an injected failure (`Faults(0, 1.0)`) is 500; a deadline header of 0 is 504; the admin route changes latency and fail rate and keeps what is not sent (`Faults(1500, 0.3)`), and an out-of-range rate is 422; business requests are counted but health and admin calls are not (`calls_received_total{route="/stock"} 1.0`); the response carries a 32-character trace id; settings defaults (16, 50, 0.0) and validation.

```python
import os

import pytest_asyncio

from chassis.postgres import create_pool


@pytest_asyncio.fixture
async def pool():
    password = os.environ["STOCK_APP_PASSWORD"]
    port = os.environ.get("STOCK_DB_PORT", "43432")
    pool = await create_pool(f"postgresql://stock_app:{password}@127.0.0.1:{port}/stock", name="it", wait_seconds=10)
    yield pool
    await pool.close()
```

An async fixture builds the pool as the application role and closes it after the test (`@pytest_asyncio.fixture`, see `pytest-asyncio.md`).

```python
import pytest
from psycopg import errors

from stock.infrastructure.postgres import PostgresStockRepository

pytestmark = pytest.mark.integration


async def test_the_seeded_skus_have_stock_and_an_unknown_sku_has_none(pool):
    repository = PostgresStockRepository(pool)
    assert await repository.quantity("sku-001") == 23
    assert await repository.quantity("sku-005") == 6
    assert await repository.quantity("sku-404") is None


async def test_the_application_role_can_read_but_not_write(pool):
    async with pool.connection() as connection:
        with pytest.raises(errors.InsufficientPrivilege):
            await connection.execute("UPDATE stock_levels SET quantity = 0")
```

The seeded skus have the expected stock (`sku-001` 23, `sku-005` 6), an unknown sku returns `None`, and the application role can read but not write (`errors.InsufficientPrivilege`).

### `tests/system/chain.py`

```python
"""The four real applications wired to each other in one process (no containers, no network).

Each hop's HTTP client talks to the next application through an ASGI transport, so the real middleware, the real use
cases and the real policy objects run; only the stock store's database is replaced by a dictionary.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from chassis.upstream import HopClient
from gateway.application.handle_search import HandleSearch
from gateway.infrastructure.http_search import HttpSearchUpstream
from gateway.infrastructure.telemetry import PrometheusTelemetry as GatewayTelemetry
from gateway.interfaces.api import create_app as create_gateway
from pricing.application.quote_price import QuotePrice
from pricing.infrastructure.cache import InMemoryQuoteCache
from pricing.infrastructure.http_stock import HttpStockUpstream
from pricing.infrastructure.telemetry import PrometheusTelemetry as PricingTelemetry
from pricing.interfaces.api import create_app as create_pricing
from resilience.backoff import Backoff
from resilience.breaker import CircuitBreaker
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import Profile, ProfileHolder
from resilience.retry_budget import RetryBudget
from search.application.search_products import SearchProducts
from search.infrastructure.http_pricing import HttpPricingUpstream
from search.infrastructure.telemetry import PrometheusTelemetry as SearchTelemetry
from search.interfaces.api import create_app as create_search
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults
from stock.infrastructure.fault_state import FaultState
from stock.interfaces.api import create_app as create_stock

```

The module docstring: the four real applications wired to each other in one process; each hop's client talks to the next app through an ASGI transport, so the real middleware, use cases and policy objects run; only the stock database is replaced by a dictionary. The imports bring in every layer's pieces; `as` renames duplicates (`PrometheusTelemetry as GatewayTelemetry`).

```python
class DictStock:
    """The stock repository, in memory: 20 skus, all in stock."""

    async def quantity(self, sku: str) -> int | None:
        return 20 if sku.startswith("sku-") else None


@dataclass
class Chain:
    """The four applications, their metrics and stores, the shared profile switch and the stock faults."""

    gateway: httpx.AsyncClient
    stores: dict[str, SpanStore]
    metrics: dict[str, ServiceMetrics]
    faults: FaultState
    profiles: dict[str, ProfileHolder] = field(default_factory=dict)
    breaker: CircuitBreaker | None = None
    limiter: ConcurrencyLimiter | None = None

    def set_profile(self, profile: Profile) -> None:
        for holder in self.profiles.values():
            holder.set(profile)

```

`DictStock` is the in-memory repository: 20 units for any sku starting with `sku-`, `None` otherwise. `Chain` is a dataclass bundling the gateway client, span stores, metrics, the fault state, profile holders, the breaker and the limiter; `set_profile` applies one profile to all holders.

```python
def build_chain(*, faults: Faults | None = None, search_limit: int = 40, capacity: int = 16, seed: int = 1) -> Chain:
    rng = random.Random(seed)
    metrics = {name: ServiceMetrics(name) for name in ("gateway", "search", "pricing", "stock")}
    stores = {name: SpanStore() for name in metrics}
    fault_state = FaultState(faults or Faults(0, 0.0))

    stock_app = create_stock(
        CheckStock(DictStock(), fault_state.get, asyncio.Semaphore(capacity), clock=time.monotonic, rand=rng.random),
        fault_state,
        HealthRegistry(),
        metrics["stock"],
        stores["stock"],
    )

    def client(app: object, caller: str, target: str) -> HopClient:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        inner = httpx.AsyncClient(transport=transport, base_url=f"http://{target}")
        return HopClient(
            caller, target, f"http://{target}", metrics=metrics[caller], store=stores[caller], client=inner
        )

    holders = {name: ProfileHolder() for name in ("gateway", "search", "pricing")}
    breaker = CircuitBreaker(time.monotonic, failure_ratio=0.5, min_calls=10, window_seconds=10.0, open_seconds=5.0)
    pricing_app = create_pricing(
        QuotePrice(
            HttpStockUpstream(client(stock_app, "pricing", "stock")),
            InMemoryQuoteCache(time.monotonic),
            breaker,
            holders["pricing"].get,
            time.monotonic,
            PricingTelemetry(metrics["pricing"]),
        ),
        holders["pricing"],
        HealthRegistry(),
        metrics["pricing"],
        stores["pricing"],
    )
    limiter = ConcurrencyLimiter(search_limit)
    search_app = create_search(
        SearchProducts(
            HttpPricingUpstream(client(pricing_app, "search", "pricing")),
            limiter,
            holders["search"].get,
            time.monotonic,
            SearchTelemetry(metrics["search"]),
        ),
        holders["search"],
        HealthRegistry(),
        metrics["search"],
        stores["search"],
    )
    gateway_app = create_gateway(
        HandleSearch(
            HttpSearchUpstream(client(search_app, "gateway", "search")),
            holders["gateway"].get,
            RetryBudget(time.monotonic, ratio=0.1, window_seconds=10.0, min_retries=3),
            Backoff(base=0.001, cap=0.002),
            time.monotonic,
            GatewayTelemetry(metrics["gateway"]),
        ),
        holders["gateway"],
        HealthRegistry(),
        metrics["gateway"],
        stores["gateway"],
        sample_rate=0.0,
    )
    gateway = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway", timeout=30)
    return Chain(gateway, stores, metrics, fault_state, holders, breaker, limiter)
```

`build_chain(...)` builds the stock app first, then pricing, search and the gateway, each with a `HopClient` to the next app over `httpx.ASGITransport` (`client(app, caller, target)`); `random.Random(seed)` is a seeded random generator; `Backoff(base=0.001, cap=0.002)` makes retries nearly instant; the breaker and limiter objects are kept so tests can inspect them. The last line builds the client the tests call: an `httpx.AsyncClient` over `ASGITransport(app=gateway_app)`.

```python
def received(chain: Chain, service: str) -> float:
    routes = {"gateway": "/search", "search": "/search", "pricing": "/price", "stock": "/stock"}
    return float(chain.metrics[service].received.labels(routes[service])._value.get())
```

`received(chain, service)` reads a service's `calls_received_total` for its business route from the Prometheus counter (`_value.get()` is the library's internal way to read a counter's number).

### `tests/system/test_chain_in_process.py`

```python
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

```

`calls(chain, n)` sends `n` searches in sequence. The first test sends a forced-trace request in the `deadline-retry` profile, reads each service's stored spans for that trace and asserts that the deadline recorded at gateway, search, pricing and stock is strictly decreasing and at most 2000 ms: the shrinking deadline.

```python
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
```

With the naive profile and a stock store that always fails, five requests become 5, 15, 45 and 135 calls at gateway, search, pricing and stock (the multiplication by three at each hop). With the policy, forty requests cost exactly forty calls at the stock store and all are degraded 200s. With only the gateway retrying, search receives at most the requests plus the 10 percent budget. With the full profile the breaker opens after about ten calls: the stock store receives between 10 and 12 of 60 requests and the breaker state is `OPEN`. With a limit of 2 and slow pricing, twelve concurrent requests (`asyncio.gather`) give some 200s and some 503s with `retry-after: 1`. A healthy chain answers every profile with fresh prices.

### `tests/system/test_policy_design.py`

```python
"""Lesson step 1 made executable: docs/design/policy.md against the numbers in the code."""

import re
from pathlib import Path

from gateway.application import handle_search
from pricing.application import quote_price
from search.application import search_products

DOC = (Path(__file__).resolve().parents[2] / "docs" / "design" / "policy.md").read_text()
TABLE = DOC.split("<!-- policy-table:begin -->")[1].split("<!-- policy-table:end -->")[0]
ROWS = [
    [cell.strip() for cell in line.strip().strip("|").split("|")]
    for line in TABLE.splitlines()
    if line.startswith("|") and not line.startswith("| ---") and not line.startswith("| Hop")
]
DEADLINE_MS = int(re.search(r"\*\*(\d+) ms\*\*", DOC).group(1))  # type: ignore[union-attr]
MARGIN_MS = int(re.findall(r"\*\*(\d+) ms\*\*", DOC)[1])
MIN_USEFUL_MS = int(re.findall(r"\*\*(\d+) ms\*\*", DOC)[2])
TIMEOUTS_MS = {row[0]: int(row[1]) for row in ROWS}


def test_the_per_hop_timeouts_add_up_to_less_than_the_end_to_end_deadline_and_nest():
    gateway, search, pricing = (TIMEOUTS_MS[h] for h in ("gateway to search", "search to pricing", "pricing to stock"))
    assert gateway < DEADLINE_MS
    assert search + MARGIN_MS <= gateway
    assert pricing + MARGIN_MS <= search


def test_exactly_one_layer_is_allowed_to_retry_and_it_is_the_gateway():
    retrying = [row[0] for row in ROWS if row[2].lower().startswith("yes")]
    assert retrying == ["gateway to search"]


def test_every_hop_says_what_it_returns_when_its_dependency_is_down():
    assert all(len(row[3]) > 10 for row in ROWS)


def test_the_numbers_in_the_code_are_the_numbers_on_the_page():
    assert round(handle_search.END_TO_END_DEADLINE * 1000) == DEADLINE_MS
    assert round(handle_search.HOP_TIMEOUT * 1000) == TIMEOUTS_MS["gateway to search"]
    assert round(search_products.HOP_TIMEOUT * 1000) == TIMEOUTS_MS["search to pricing"]
    assert round(quote_price.HOP_TIMEOUT * 1000) == TIMEOUTS_MS["pricing to stock"]
    for module in (search_products, quote_price):
        assert round(module.MARGIN * 1000) == MARGIN_MS
        assert round(module.MIN_USEFUL * 1000) == MIN_USEFUL_MS
```

Checks `docs/design/policy.md` against the code. `DOC` is the page; `TABLE` is the text between two HTML comment markers; `ROWS` is a list comprehension that turns each table line into cells. Regular expressions pull the three millisecond figures in bold (`\*\*(\d+) ms\*\*`) from the page. Tests: the per-hop timeouts nest (gateway below the deadline; each next timeout plus the margin fits inside the previous); exactly one layer may retry and it is the gateway; every hop says what it returns when its dependency is down; and the numbers in the code (`handle_search.END_TO_END_DEADLINE`, each module's `HOP_TIMEOUT`, `MARGIN`, `MIN_USEFUL`) equal the numbers on the page.

### `tests/system/test_harness.py`

```python
"""The measuring tools themselves: window statistics, trace rendering and the recovery rule."""

from typing import Any

from tests.system import harness
from tests.system.experiments import _recovered


def recorder(samples):
    r = harness.LoadRecorder(t0=100.0)
    r.samples = [
        harness.Sample(100.0 + start, latency, status, degraded) for start, latency, status, degraded in samples
    ]
    return r


```

`recorder(samples)` builds a `LoadRecorder` with a start time of 100.0 and samples given as `(start offset, latency, status, degraded)` tuples.

```python
def test_window_statistics_count_errors_and_degraded_answers_separately_and_use_the_requested_window():
    r = recorder(
        [
            (0.5, 0.1, 200, False),
            (1.0, 0.2, 200, True),
            (1.5, 2.0, 503, False),
            (2.5, 0.3, 200, False),
            (9.0, 5.0, 0, False),
        ]
    )
    window = r.window(0, 2)
    assert window["requests"] == 3
    assert window["error_rate"] == round(1 / 3, 4)
    assert window["degraded_rate"] == round(1 / 3, 4)
    assert window["p99_ms"] == 2000
    assert window["statuses"] == {200: 2, 503: 1}
    assert r.window(8, 10)["statuses"] == {0: 1}


def test_an_empty_window_reports_none_instead_of_dividing_by_zero():
    assert recorder([]).window(0, 10)["requests"] == 0
    assert recorder([]).window(0, 10)["p99_ms"] is None

```

Window statistics count errors and degraded answers separately and use the requested window (the 99th-percentile latency and the status counts); an empty window reports zero requests and `None` instead of dividing by zero.

```python
def test_the_trace_tree_indents_children_under_their_parents_and_shows_the_attempt():
    spans: list[dict[str, Any]] = [
        {
            "spanId": "a",
            "parentId": None,
            "name": "gateway /search",
            "startMs": 1000.0,
            "durationMs": 50.0,
            "status": "200",
            "attributes": {},
        },
        {
            "spanId": "b",
            "parentId": "a",
            "name": "call search",
            "startMs": 1001.0,
            "durationMs": 40.0,
            "status": "200",
            "attributes": {"attempt": 1},
        },
        {
            "spanId": "c",
            "parentId": "b",
            "name": "search /search",
            "startMs": 1002.0,
            "durationMs": 30.0,
            "status": "200",
            "attributes": {},
        },
    ]
    lines = harness.render_trace(spans).splitlines()
    assert [len(line) - len(line.lstrip()) for line in lines] == [0, 2, 4]
    assert "attempt=1" in lines[1]
    assert harness.render_trace([]) == "(no spans recorded)"

```

The trace tree indents children under their parents (indent 0, 2, 4 spaces), shows `attempt=1`, and prints `(no spans recorded)` for an empty list.

```python
def test_the_recovery_rule_needs_clean_fast_answers_and_a_drained_stock_store():
    good = recorder([(t / 2, 0.05, 200, False) for t in range(0, 40)])
    flights_low = [(t / 2, 2.0) for t in range(0, 40)]
    assert _recovered(good, flights_low, 20.0, threshold=10.0)
    assert _recovered(
        recorder([(t / 2, 0.05, 200, True) for t in range(0, 40)]),
        flights_low,
        20.0,
        threshold=10.0,
        baseline={"degraded_rate": 0.9},
    )
    assert not _recovered(good, [(t / 2, 50.0) for t in range(0, 40)], 20.0, threshold=10.0)
    assert not _recovered(good, [], 20.0, threshold=10.0)
    assert not _recovered(recorder([(t / 2, 0.05, 200, True) for t in range(0, 40)]), flights_low, 20.0, threshold=10.0)
    assert not _recovered(recorder([(t / 2, 0.9, 200, False) for t in range(0, 40)]), flights_low, 20.0, threshold=10.0)
    assert not _recovered(
        recorder([(t / 2, 0.05, 503 if t in (28, 30) else 200, False) for t in range(0, 40)]),
        flights_low,
        20.0,
        threshold=10.0,
    )


def test_metric_lookup_builds_the_key_from_sorted_labels():
    values = {"calls_received_total{route=/stock}": 7.0, "in_flight": 3.0}
    assert harness.metric(values, "calls_received_total", route="/stock") == 7.0
    assert harness.metric(values, "in_flight") == 3.0
    assert harness.metric(values, "missing") == 0.0


def test_the_recovery_rule_accepts_a_slower_p99_when_the_baseline_was_slower():
    slow_but_clean = recorder([(t / 2, 0.9, 200, False) for t in range(0, 40)])
    flights = [(t / 2, 2.0) for t in range(0, 40)]
    assert not _recovered(slow_but_clean, flights, 20.0, threshold=10.0)
    assert _recovered(slow_but_clean, flights, 20.0, threshold=10.0, p99_limit_ms=1800)
```

The recovery rule: clean fast answers and a drained stock store count as recovered (also when the baseline itself had degraded answers); a stock store with 50 in flight, no samples of the stock store, degraded answers, slow answers (0.9 s) or errors in the last window do not. Metric lookup builds its key from sorted labels and returns 0.0 when absent. A slower p99 limit accepts a slower but clean baseline.

### `tests/system/harness.py`

```python
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
```

Imports, including `text_string_to_metric_families` from `prometheus_client.parser` (it parses the Prometheus text format; see `prometheus-client.md`). `ROOT` is the repository folder. `url(service)` builds a service's address from a port variable with a default. `compose(*args)` runs `docker compose` from the repository folder with `subprocess.run([...], cwd=ROOT, capture_output=True, text=True, check=False, timeout=600)`.

```python
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
```

`wait_ready` polls each service's `/health/ready` until it answers 200 (an `httpx.HTTPError` while waiting is ignored) or raises `TimeoutError`. `restart_chain` restarts the four services so every counter, cache, breaker and queue starts empty. `set_profile` posts the profile name to the three services that have profiles and calls `raise_for_status()`. `set_faults` posts only the fields that are not `None` (a dictionary comprehension).

```python
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
```

`scrape(service)` fetches `/metrics` and parses it into a dictionary from `"name{label=value,...}"` to a number, skipping the `_created` and `_bucket` series; an unreachable service gives `{}` (the `finally` closes a client the function created). `metric(values, name, **labels)` builds the same key from sorted labels and returns 0.0 when absent. `received_counts` reads `calls_received_total` for each service's business route.

```python
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
```

`Sample` is one request as the load generator saw it. `LoadRecorder.window(start, end)` filters the samples that started in the window and returns the count, the median and 99th-percentile latency in milliseconds (the sorted latencies indexed at 99 percent), the error rate (status not 200), the degraded rate and a `Counter` of statuses; an empty window returns `None`s.

```python
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
```

`generate_load` is an **open-loop** load generator: it starts one request every `1/rps` seconds whether or not earlier ones have finished (a closed-loop generator would wait, and hide the storm). It makes a client with room for 5000 connections, then loops: `asyncio.create_task(one(n))` starts a request in the background; `tasks.add(task)` and `task.add_done_callback(tasks.discard)` keep a reference and forget it when done; `next_at = recorder.t0 + n / rps` and `await asyncio.sleep(max(0.0, next_at - time.monotonic()))` keep the pace even if a step is slow. `one(n)` records the latency and status, or status 0 on a network error. After the stop flag it waits up to 30 seconds for stragglers. `sample_stock_in_flight` records every half second the stock store's `in_flight` gauge.

```python
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
```

`render_trace` turns spans into an indented tree: spans are grouped by parent (`by_parent.setdefault(parent, []).append(span)`), and the recursive inner function `walk` prints each with its name (`:<22` pads to 22 characters), start offset and duration (`:8.1f` is a number with one decimal in 8 columns) and status. `fetch_trace` collects a trace's spans from all four services' admin routes. `forced_request` sends one search with `X-Trace-Force` and returns the trace id and a summary.

### `tests/system/experiments.py`

```python
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

from tests.system import harness

RESULTS = harness.ROOT / "docs" / "results"
RPS = float(os.environ.get("LOAD_RPS", "5"))


def _save(name: str, data: dict[str, Any], traces: dict[str, str] | None = None) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    for label, text in (traces or {}).items():
        (RESULTS / f"{name}-trace-{label}.txt").write_text(text + "\n")


async def _prepare(profile: str, latency_ms: int, fail_rate: float) -> None:
    await harness.restart_chain()
    await harness.set_profile(profile)
    await harness.set_faults(latency_ms=latency_ms, fail_rate=fail_rate)
    await asyncio.sleep(2)


async def _start_load(rps: float) -> tuple[harness.LoadRecorder, asyncio.Event, asyncio.Task[None]]:
    recorder, stop = harness.LoadRecorder(), asyncio.Event()
    task = asyncio.create_task(harness.generate_load(recorder, rps, stop))
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
```

The docstring: every experiment restarts the services, sets the profile and the faults, drives the gateway with an open-loop load and reports what it measured. `RPS` reads `LOAD_RPS` (default 5). `_save` writes `docs/results/<name>.json` (`json.dumps(data, indent=2)`) and one text file per trace. `_prepare` restarts and configures. `_start_load` starts the generator and waits until it has started. `_ratios` divides each service's count by the gateway's (`or 1.0` avoids dividing by zero).

```python
async def amplification(
    profile: str, fail_rate: float = 0.3, latency_ms: int = 50, seconds: int = 45
) -> dict[str, Any]:
    """Steps 2 and 4: count the calls arriving at each layer for the same number of gateway requests."""
    await _prepare(profile, latency_ms, fail_rate)
    before = await harness.received_counts()
    recorder, stop, task = await _start_load(RPS)
    await asyncio.sleep(seconds)
    stop.set()
    await task
    await asyncio.sleep(5)
    after = await harness.received_counts()
    received = {s: after[s] - before[s] for s in harness.CHAIN}
    gateway_metrics = await harness.scrape("gateway")
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
    print("   " + "   ".join(f"{s}: {received[s]:.0f}" for s in harness.CHAIN))
    print(
        f"   ratio stock:gateway = {result['ratios']['stock:gateway']}x   (search {result['ratios']['search:gateway']}x, pricing {result['ratios']['pricing:gateway']}x)"
    )
    print(f"   client: {result['client']}")
    _save(f"amplification-{profile}", result)
    return result
```

`amplification` (steps 2 and 4): read the counters, run the load for 45 seconds, wait five seconds for stragglers, read the counters again and subtract; build the result dictionary (`sum(v for k, v in gateway_metrics.items() if k.startswith("retries_total{"))` adds the gateway's retries over all labels); print and save.

```python
def _recovered(
    recorder: harness.LoadRecorder,
    in_flight: Sequence[tuple[float, float | None]],
    now: float,
    threshold: float,
    p99_limit_ms: float = 600,
    baseline: dict[str, Any] | None = None,
) -> bool:
    """True when the last 10 seconds look like the baseline again: the stock store has drained, answers are as fast and
    as clean as before the slowdown (the baseline keeps its injected failures, so "clean" is relative to it)."""
    stats = recorder.window(now - 10, now)
    flights = [v for t, v in in_flight if now - 10 <= t < now]
    base_errors = (baseline or {}).get("error_rate") or 0.0
    base_degraded = (baseline or {}).get("degraded_rate") or 0.0
    return bool(
        stats["requests"]
        and stats["error_rate"] <= base_errors + 0.05
        and stats["degraded_rate"] <= base_degraded + 0.2
        and stats["p99_ms"] <= p99_limit_ms
        and flights
        and all(v is not None and v <= threshold for v in flights)
    )
```

`_recovered` decides whether the last ten seconds look like the baseline again: requests exist; the error rate is at most the baseline's plus 0.05; the degraded rate at most the baseline's plus 0.2; the 99th percentile within the limit; and the stock store's in-flight count has drained (every sample at most the threshold). `bool(...)` of a chain of `and` returns a real boolean.

```python
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
    sampler = asyncio.create_task(harness.sample_stock_in_flight(in_flight, recorder.t0, stop))

    def elapsed() -> float:
        return time.monotonic() - recorder.t0

    await asyncio.sleep(baseline_s)
    trace_baseline = await harness.forced_request()
    t_slow = elapsed()
    await harness.set_faults(latency_ms=slow_ms)
    await asyncio.sleep(slow_s / 2)
    during_task = asyncio.create_task(harness.forced_request())
    await asyncio.sleep(slow_s / 2)
    t_reset = elapsed()
    await harness.set_faults(latency_ms=50)
    during = await asyncio.wait_for(during_task, 60)
    baseline_peak = max((v for t, v in in_flight if 5 <= t < t_slow and v is not None), default=0.0)
    threshold = max(3 * baseline_peak, 10.0)
    baseline = recorder.window(5, t_slow)
    p99_limit_ms = max(600.0, 2.0 * (baseline["p99_ms"] or 0))
    recovered_at: float | None = None
    clean_checks = 0
    while elapsed() < t_reset + recover_cap_s:
        await asyncio.sleep(1)
        clean_checks = (
            clean_checks + 1 if _recovered(recorder, in_flight, elapsed(), threshold, p99_limit_ms, baseline) else 0
        )
        if clean_checks == 2:
            recovered_at = elapsed() - t_reset - 1
            break
    stop.set()
    await load
    await sampler
    during_flights = [v for t, v in in_flight if t_slow + 5 <= t < t_reset and v is not None]
    after_flights = [(round(t - t_reset), v) for t, v in in_flight if t >= t_reset and v is not None][::6]
    result: dict[str, Any] = {
        "profile": profile,
```

`slowdown` (steps 3 and 6): prepare, start the load and a sampler of the stock store's in-flight count, wait 20 seconds (baseline), take a forced trace, inject 1500 ms of latency, wait, take another forced trace during the slowdown, remove the latency, then poll once a second for up to 240 seconds. It computes a threshold (`max(3 * baseline_peak, 10.0)`) and a latency limit from the baseline, and declares recovery after two clean checks in a row. `elapsed()` is a nested function over the recorder's start time.

```python
    during_flights = [v for t, v in in_flight if t_slow + 5 <= t < t_reset and v is not None]
    after_flights = [(round(t - t_reset), v) for t, v in in_flight if t >= t_reset and v is not None][::6]
    result: dict[str, Any] = {
        "profile": profile,
        "fail_rate": fail_rate,
        "slow_ms": slow_ms,
        "rps": RPS,
        "baseline": baseline,
        "slowdown": recorder.window(t_slow + 5, t_reset),
        "stock_in_flight": {
            "baseline_peak": baseline_peak,
            "slowdown_peak": max(during_flights, default=0),
            "after_reset_every_3s": after_flights,
        },
        "after_reset_windows": [
            {"from_s": start, **recorder.window(t_reset + start, t_reset + start + 10)}
            for start in range(0, int(elapsed() - t_reset), 10)
        ],
        "recovery_seconds": None if recovered_at is None else round(recovered_at, 1),
        "recovery_capped_at_s": recover_cap_s,
        "recovery_threshold_in_flight": threshold,
        "recovery_p99_limit_ms": p99_limit_ms,
        "traces": {"baseline": trace_baseline[1], "slowdown": during[1]},
    }
    traces = {
        "baseline": harness.render_trace(await harness.fetch_trace(trace_baseline[0])),
        "slowdown": harness.render_trace(await harness.fetch_trace(during[0])),
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
        f"   recovery after the latency was removed: {result['recovery_seconds']} s (cap {recover_cap_s} s, in-flight threshold {threshold:g}, p99 limit {p99_limit_ms:g} ms)"
    )
    _save(f"slowdown-{profile}", result, traces)
    return result
```

It collects the in-flight samples during and after the slowdown (`[::6]` takes every sixth item), builds the result with the baseline, slowdown and after-reset windows (ten-second windows: `range(0, int(elapsed() - t_reset), 10)`), renders both traces, counts the stock spans, prints a summary and saves.

```python
async def outage(seconds_before: int = 15, seconds_down: int = 30) -> dict[str, Any]:
    """Step 5: stop the stock store entirely under the full profile; the gateway must keep answering inside its deadline."""
    await _prepare("full", 50, 0.0)
    recorder, stop, load = await _start_load(RPS)
    await asyncio.sleep(seconds_before)
    pricing_before = await harness.scrape("pricing")
    harness.compose("stop", "stock")
    t_down = time.monotonic() - recorder.t0
    await asyncio.sleep(seconds_down)
    pricing_during = await harness.scrape("pricing")
    trace_id, summary = await harness.forced_request()
    trace = harness.render_trace(await harness.fetch_trace(trace_id))
    t_up = time.monotonic() - recorder.t0
    harness.compose("start", "stock")
    await harness.wait_ready(("stock",))
    await asyncio.sleep(12)
    stop.set()
    await load
    sent = harness.metric(pricing_during, "calls_sent_total", target="stock") - harness.metric(
        pricing_before, "calls_sent_total", target="stock"
    )
    rejected = harness.metric(pricing_during, "breaker_rejected_total") - harness.metric(
        pricing_before, "breaker_rejected_total"
    )
    requests_during = recorder.window(t_down, t_up)["requests"]
    result = {
        "rps": RPS,
        "before_outage": recorder.window(3, t_down),
        "during_outage": recorder.window(t_down + 6, t_up),
        "after_restart": recorder.window(t_up + 8, t_up + 12),
        "requests_during_outage": requests_during,
        "pricing_attempts_sent_to_stock_during_outage": sent,
        "pricing_calls_refused_by_the_breaker": rejected,
        "breaker_state_at_end_of_outage": harness.metric(pricing_during, "breaker_state"),
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
```

`outage` (step 5): with the full profile and steady load, read pricing's counters, stop the stock container with `compose("stop", "stock")`, wait 30 seconds, read again, take a forced trace, start stock again and wait until it is ready, then compute how many attempts pricing sent to the dead store and how many calls the open breaker refused locally.

```python
async def shedding(rps: float = 10.0, limit: int = 3, seconds: int = 30) -> dict[str, Any]:
    """Step 5, second half: with search's concurrency limit small and pricing slow, the excess is refused at once."""
    os.environ["SEARCH_CONCURRENCY_LIMIT"] = str(limit)
    harness.compose("up", "-d", "--force-recreate", "--no-deps", "search")
    try:
        await _prepare("full", 1500, 0.0)
        await harness.set_profile("full")
        # Take the breaker out of the picture so pricing keeps calling the slow store and search's in-flight count climbs.
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{harness.url('pricing')}/admin/profile", json={"name": "deadline-retry"})
        recorder, stop, load = await _start_load(rps)
        await asyncio.sleep(seconds)
        stop.set()
        await load
        search_metrics = await harness.scrape("search")
        gateway_metrics = await harness.scrape("gateway")
        window = recorder.window(5, seconds)
        result = {
            "rps": rps,
            "limit": limit,
            "client": window,
            "search_shed": harness.metric(search_metrics, "shed_total"),
            "gateway_retries": sum(v for k, v in gateway_metrics.items() if k.startswith("retries_total{")),
            "gateway_outcomes": {k: v for k, v in gateway_metrics.items() if k.startswith("outcomes_total{")},
        }
    finally:
        os.environ.pop("SEARCH_CONCURRENCY_LIMIT", None)
        harness.compose("up", "-d", "--force-recreate", "--no-deps", "search")
        await harness.wait_ready(("search",))
    print(f"search limited to {limit} in flight, pricing slow, {rps:g} requests/s for {seconds} s")
    print(
        f"   shed by search: {result['search_shed']:.0f}; gateway retries: {result['gateway_retries']:.0f}; client view: {window}"
    )
    _save("shedding-full", result)
    return result
```

`shedding` (step 5, second half): sets the search concurrency limit through an environment variable and recreates the search container so it picks it up, makes stock slow (1500 ms) and pricing take the `deadline-retry` profile (so the breaker does not hide the effect), drives 10 requests per second for 30 seconds and reads search's `shed_total` and the gateway's retry and outcome counters; the `finally` removes the variable and recreates search with the normal limit.

### `tests/e2e/test_chain.py`

```python
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

```

The chain across the running containers. `pytestmark = pytest.mark.e2e` marks the file. The fixture `clean_chain` is `autouse=True` (it runs before every test without being named) and restarts the chain, sets the naive profile and resets the faults. `search(client, **kwargs)` is a helper.

```python
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

```

A forced-trace request crosses all four services and the trace tree contains the seven expected names (`call search`, `search /search`, ...); the trace id is known to every service's admin route; a parametrized test checks that each profile answers a healthy chain with ten fresh answers.

```python
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
```

With the full profile and the stock container stopped (`harness.compose("stop", "stock")`; `finally` starts it again), the gateway still answers 200 with degraded stale prices; the stock store refuses (504 in under 0.6 seconds) work that cannot finish inside a 200 ms deadline when its latency is 800 ms; the admin route switches profiles and rejects unknown ones.

## Try it yourself

1. Run the policy library's tests with coverage:

   ```bash
   cd libs/resilience && uv run pytest tests/unit -q --cov=resilience --cov-report=term-missing:skip-covered
   ```

   Expect `33 passed` and a `TOTAL` of about 99 percent (`retry.py` has one partially covered branch).

2. Run the in-process chain tests:

   ```bash
   uv run pytest tests/system/test_chain_in_process.py -q
   ```

   Expect `9 passed`.

3. Run the measuring tools' own tests and the policy-page check:

   ```bash
   uv run pytest tests/system -q
   ```

   Expect `19 passed`.

4. List the tests in a folder without running them:

   ```bash
   cd services/search && uv run pytest tests/unit --collect-only -q | tail -3
   ```

   Expect test names and `12 tests collected`.

## Mistakes people make

- **Tests that depend on each other's order or leftover data.** Each test should create what it needs.
- **Asserting too little.** A test that calls the code and checks nothing always passes.
- **Chasing 100 percent coverage everywhere.** Coverage shows what was not run; it does not prove the checks are good. This repository enforces 95 percent only where the rules live: the domain and application layers (and, where there is one, the library of pure policy objects).
- **Real clocks, randomness and sleeps in unit tests.** Pass them in as parameters and fake them.
- **Forgetting to register a marker.** With `--strict-markers` that is an error, on purpose.

## Check yourself

1. How does pytest know which functions are tests?
   Answer: files named `test_*.py` and functions named `test_*`.
2. What is a fixture?
   Answer: a function that prepares something for tests; tests receive it by naming it as an argument.
3. What does `-m "not integration"` do?
   Answer: runs all tests except those marked `integration`.
4. What does `--fail-under=95` do?
   Answer: makes the coverage report fail when coverage is below 95 percent.
5. Unit test or integration test: a test that needs a running PostgreSQL?
   Answer: integration test.

## Further reading

- pytest documentation (this repository pins pytest 9.1): https://docs.pytest.org/en/stable/
- Fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- Markers: https://docs.pytest.org/en/stable/how-to/mark.html
- pytest-cov (pinned 7.1): https://pytest-cov.readthedocs.io/en/latest/
