# httpx

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

`httpx` is a Python library for making HTTP requests: your code acts as the client and calls another program's web API. It has a simple API (`client.post(url, json=body)`), supports timeouts, and has both a synchronous and an asynchronous client.

## Why this repository uses it

The chain is four services calling each other over HTTP, so the call between two services is the object of study. `HopClient` wraps the asynchronous `httpx.AsyncClient` and makes exactly **one** attempt per call, with a hard timeout, the trace header and the deadline header. The retry policy lives elsewhere (the gateway), which is the lesson. The experiments also use httpx as the load generator and the control client.

## Where it appears

| Path | What it does with httpx |
| --- | --- |
| `libs/chassis/src/chassis/upstream.py` | `HopClient`: one attempt at one call |
| `services/*/src/*/infrastructure/http_*.py` | Adapters that use `HopClient` to reach the next service |
| `tests/system/harness.py`, `experiments.py` | The load generator, profile switch, fault injection, metric reads |
| `tests/system/chain.py` | `ASGITransport`: the four apps call each other in memory |
| `pyproject.toml`, `libs/chassis/pyproject.toml` | The dependency `httpx>=0.27` |

## How it works underneath

- A `Client` object owns a **connection pool**: it keeps connections to a server open and reuses them for later requests, which is faster than opening a new one each time. You create one client and reuse it; closing it releases the connections.
- `base_url` is prepended to every path you pass. `json=body` converts a dictionary to JSON text and sets the `Content-Type` header.
- A **timeout** limits how long a step may take. `httpx.Timeout(2.0)` sets 2 seconds for connecting, for sending, for reading and for taking a connection from the pool. Without a timeout a call to a hung server would wait for ever (httpx's own default is 5 seconds, which this code overrides).
- Failures show up as exceptions. `httpx.TransportError` is the parent of connection and timeout errors; a response with status 500 is **not** an exception, you read `response.status_code`.
- A **transport** is the part that actually sends the request. `httpx.MockTransport(handler)` replaces it with a function you write, so tests can pretend to be a server without a network.

This repository uses the **asynchronous** client: `httpx.AsyncClient`, used with `await client.post(...)` and `async with`. It lets one process have thousands of calls in progress.

## Walkthrough of this repository's use

### One attempt

```python
    def __init__(
        self,
        service: str,
        target: str,
        base_url: str,
        *,
        metrics: ServiceMetrics,
        store: SpanStore,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._target = target
        self._metrics = metrics
        self._store = store
        self._clock = clock
        limits = httpx.Limits(max_connections=2000, max_keepalive_connections=200)
        self._client = client or httpx.AsyncClient(base_url=base_url, limits=limits)

    async def call(
        self, path: str, body: dict[str, Any], *, timeout: float, deadline: Deadline | None, attempt: int = 1
    ) -> dict[str, Any]:
        """One POST of ``body``; return the JSON answer or raise :class:`AttemptFailed`."""
        self._metrics.sent.labels(self._target).inc()
```

- The constructor takes everything it needs, including an optional ready-made `client` and the `clock`, so tests can replace them.
- `httpx.Limits(max_connections=2000, max_keepalive_connections=200)` raises the pool size well above httpx's default: in a storm there really are thousands of calls in flight, and the client must not become the bottleneck that hides the effect being studied.

```python
    async def call(
        self, path: str, body: dict[str, Any], *, timeout: float, deadline: Deadline | None, attempt: int = 1
    ) -> dict[str, Any]:
        """One POST of ``body``; return the JSON answer or raise :class:`AttemptFailed`."""
        self._metrics.sent.labels(self._target).inc()
        parent = current_span() or new_trace(False)
        context = child_of(parent)
        headers = {TRACEPARENT: context.header()}
        if deadline is not None:
            headers[DEADLINE_HEADER] = deadline.header_value(self._clock())
        with SpanRecorder(
            self._store,
            self._service,
            f"call {self._target}",
            context,
            parent.span_id,
            {"attempt": attempt, "timeoutMs": int(timeout * 1000)},
        ) as span:
            try:
                async with asyncio.timeout(timeout):
                    response = await self._client.post(path, json=body, headers=headers)
            except TimeoutError as error:
                span.status = "timeout"
                raise AttemptFailed("timeout") from error
            except httpx.TransportError as error:
                span.status = "connect-error"
                raise AttemptFailed(type(error).__name__) from error
            span.status = str(response.status_code)
            if response.status_code >= 500 or response.status_code == 429:
                raise AttemptFailed(f"status {response.status_code}", retry_after=_retry_after(response))
            if response.status_code >= 400:
                raise AttemptFailed(f"status {response.status_code}", retryable=False)
            answer: dict[str, Any] = response.json()
            return answer
```

- `self._metrics.sent.labels(self._target).inc()` counts the attempt before it is made: this is the number the amplification measurement is built from.
- The trace header (`traceparent`) goes on every call; the deadline header (`X-Deadline-Ms`) goes on only when there is a deadline, carrying the time that is left.
- `async with asyncio.timeout(timeout)` bounds the whole call (see `asyncio.md`). `TimeoutError` becomes `AttemptFailed("timeout")`; `httpx.TransportError` (connection refused, reset) becomes `AttemptFailed(type(error).__name__)`.
- The status code decides the kind of failure: 500 and above, or 429, are retryable and carry a `Retry-After` hint; other 4xx are not retryable. Anything else returns the parsed JSON.

```python
def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None
```

`_retry_after` reads the `Retry-After` header as seconds; if it is missing or not a number, it returns `None`.

### Where it is used

```python
"""Search reached over HTTP."""

from __future__ import annotations

from chassis.upstream import HopClient
from gateway.domain.models import SearchAnswer
from resilience.deadline import Deadline


class HttpSearchUpstream:
    """One attempt at ``POST /search``; the retry policy lives in the use case."""

    def __init__(self, client: HopClient) -> None:
        self._client = client

    async def search(self, query: str, timeout: float, deadline: Deadline | None, attempt: int) -> SearchAnswer:
        """The answer to the query."""
        answer = await self._client.call(
            "/search", {"query": query}, timeout=timeout, deadline=deadline, attempt=attempt
        )
        return SearchAnswer(
            query, answer["sku"], answer["price"], answer["inStock"], bool(answer["degraded"]), bool(answer["stale"])
        )
```

The adapter implements the application's port (`SearchUpstream`) using a `HopClient`: it builds the request body, calls, and converts the JSON answer into the domain's `SearchAnswer`. The application layer never imports httpx (import-linter enforces that; see `import-linter.md`).

### The whole chain in memory

```python
    def client(app: object, caller: str, target: str) -> HopClient:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        inner = httpx.AsyncClient(transport=transport, base_url=f"http://{target}")
        return HopClient(
            caller, target, f"http://{target}", metrics=metrics[caller], store=stores[caller], client=inner
        )
```

`httpx.ASGITransport(app=app)` is a transport that calls an ASGI application directly, with no network. `httpx.AsyncClient(transport=transport, base_url=...)` is a client that talks to it. Each service gets a `HopClient` wired to the next app this way, so the full chain, with real middleware and real policy, runs in one test process.

### As the load generator

```python
async def set_profile(name: str) -> None:
    """Switch every service (except stock, which has no profile) to a resilience profile."""
    async with httpx.AsyncClient(timeout=30) as client:
        for service in ("gateway", "search", "pricing"):
            answer = await client.post(f"{url(service)}/admin/profile", json={"name": name})
            answer.raise_for_status()
```

The harness uses short-lived clients for control calls (`async with httpx.AsyncClient(timeout=30) as client:`) and `answer.raise_for_status()`, which turns an error status into an exception.

## Try it yourself

1. Make a fake server and call it asynchronously:

   ```bash
   uv run python -c "
   import asyncio, httpx
   def handler(request):
       return httpx.Response(200, json={'echo': request.url.path})
   async def main():
       async with httpx.AsyncClient(base_url='http://x', transport=httpx.MockTransport(handler)) as c:
           r = await c.post('/a', json={'k': 1})
           print(r.status_code, r.json())
   asyncio.run(main())"
   ```

   Expect `200 {'echo': '/a'}`.

2. Run the hop client's tests:

   ```bash
   cd libs/chassis && uv run pytest tests/unit -q
   ```

   Expect all to pass (timeouts become `AttemptFailed`, 5xx are retryable, 4xx are not, `Retry-After` is read).

3. With the stack up, call the gateway health route:

   ```bash
   uv run python -c "import httpx; print(httpx.get('http://127.0.0.1:43080/health/ready', timeout=5).json())"
   ```

   Expect `{'status': 'ready', 'checks': {...}}`.

## Mistakes people make

- **No timeout.** One slow server then blocks the caller for ever.
- **A new client for every call.** It throws away the connection pool.
- **Retrying calls that are not safe to repeat.** A timeout does not tell you whether the server already acted.
- **Treating 4xx and 5xx alike.** A 4xx says the request is wrong; retrying cannot help. A 5xx or a timeout may succeed later.
- **Forgetting `response.raise_for_status()`** when you want errors as exceptions. The retrying clients in this repository check status codes explicitly instead.

## Check yourself

1. Why reuse one `Client`?
   Answer: it keeps a pool of connections open, which is faster.
2. Is a 503 answer an exception in httpx?
   Answer: no; it is a normal response and you read `status_code`.
3. What does `MockTransport` give a test?
   Answer: a fake server written as a Python function, with no network.
4. What does `json=body` do?
   Answer: encodes the dictionary as JSON and sets the content type.
5. Why must a timeout always be set?
   Answer: so a hung server cannot block the caller for ever.

## Further reading

- httpx documentation (this repository pins httpx 0.28): https://www.python-httpx.org/
- Clients: https://www.python-httpx.org/advanced/clients/
- Timeouts: https://www.python-httpx.org/advanced/timeouts/
- Transports and mocking: https://www.python-httpx.org/advanced/transports/
