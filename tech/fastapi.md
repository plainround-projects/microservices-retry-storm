# FastAPI and Starlette

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

FastAPI is a Python framework for building web APIs. A **web API** is a program that other programs call over HTTP: they send a request (a method such as `GET` or `POST`, a path such as `/orders`, headers, an optional body) and get a response (a status code such as 200 or 404, headers, a body, usually JSON). **JSON** is a text format for data: objects `{"a": 1}`, lists `[1, 2]`, text, numbers, `true`, `false`, `null`.

FastAPI is built on **Starlette**, a smaller toolkit that handles the low-level web work, and uses **pydantic** (see `pydantic.md`) to check data. You write ordinary Python functions and mark them as handlers for a method and a path.

## Why this repository uses it

Each of the four services is an HTTP API that calls the next one, and the lesson is about what happens on those calls. FastAPI is asynchronous, so a service can hold thousands of waiting requests in one process, which the experiments need. Its middleware hook is where the deadline, the trace and the metrics are handled for every request.

## Where it appears

| Path | What it does with FastAPI |
| --- | --- |
| `services/gateway/src/gateway/interfaces/api.py` | `POST /search`, answering 503 with `Retry-After` when the policy gives up |
| `services/search/.../api.py`, `services/pricing/.../api.py`, `services/stock/.../api.py` | One business route each (`/search`, `/price`, `/stock`) |
| `libs/chassis/src/chassis/observability.py` | The middleware: trace, correlation id, deadline, metrics |
| `libs/chassis/src/chassis/web.py` | `/health/live`, `/health/ready` |
| `libs/chassis/src/chassis/metrics.py` | `/metrics` |
| `libs/chassis/src/chassis/admin.py` | `/admin/profile`, `/admin/traces` for the experiments |
| `services/stock/.../api.py` | `/admin/config`: inject latency and failures |
| `services/*/src/*/interfaces/bootstrap.py` | Builds the app and closes resources at shutdown |
| `tests/system/chain.py` | Runs all four apps in one process |

## How it works underneath

1. A web server (see `uvicorn.md`) accepts the network connection and reads the HTTP request.
2. The server and the application talk through **ASGI**, a standard interface: the application is a function that receives three things, `scope` (facts about the request such as its path and headers), `receive` (a way to read the body) and `send` (a way to write the response). A **middleware** is a wrapper around the application that can look at or change the request and response on the way in and out.
3. FastAPI finds the route whose method and path match. `@app.get("/orders/{order_id}")` has a **path parameter** in braces; its value is passed to the function argument of the same name.
4. FastAPI reads the function's type hints. A parameter typed as a pydantic model is read from the JSON body and **validated**: wrong types or missing fields produce an automatic `422` answer without calling your function.
5. A plain `def` handler runs on a worker thread, so a slow database call does not block other requests. An `async def` handler runs on the event loop and must not block.
6. The return value (a dict, a list, a model) is turned into JSON. To answer with an error, `raise HTTPException(status_code=404, detail="...")`.

An `APIRouter` is a group of routes defined elsewhere and added with `app.include_router`. A **lifespan** is code that runs when the application starts and stops. `TestClient` calls the application in-process, without a network, for tests.

In this repository every handler is `async def`, and no handler blocks: they await the next hop with a timeout.

## Walkthrough of this repository's use

### The edge of the chain

```python
    app = FastAPI(title="gateway", docs_url=None, redoc_url=None)
    app.add_middleware(
        ObservabilityMiddleware, service="gateway", metrics=metrics, store=store, sample_rate=sample_rate
    )
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(holder, store))

    @app.post("/search", response_model=None)
    async def search(body: SearchBody) -> dict[str, Any] | JSONResponse:
        try:
            answer = await handle(body.query)
        except Unavailable as error:
            return JSONResponse(
                {"detail": "temporarily unavailable", "reason": str(error)},
                status_code=503,
                headers={"Retry-After": str(int(error.retry_after))},
            )
        return {
            "query": answer.query,
            "sku": answer.sku,
            "price": answer.price,
            "inStock": answer.in_stock,
            "degraded": answer.degraded,
            "stale": answer.stale,
        }
```

- `FastAPI(title="gateway", docs_url=None, redoc_url=None)` creates the app with the built-in documentation off.
- `app.add_middleware(ObservabilityMiddleware, service="gateway", ...)` wraps every request; the keyword arguments are passed to the middleware's constructor.
- `app.include_router(...)` adds groups of routes: health, metrics and admin routes are shared; each service adds its business route.
- `@app.post("/search", response_model=None)` registers the function. `response_model=None` tells FastAPI not to build a response schema from the return type, because the function returns either a dictionary or a `JSONResponse`.
- `body: SearchBody` is a pydantic model (see `pydantic.md`): FastAPI answers 422 itself for a missing or empty query.
- On `Unavailable` the handler builds the response itself: `JSONResponse({...}, status_code=503, headers={"Retry-After": ...})`. The `Retry-After` header tells a well-behaved client when to try again; the gateway's own retry loop and the load generator both read it.

### The bottom of the chain

```python
def create_app(
    check_stock: CheckStock,
    fault_state: FaultState,
    health: HealthRegistry,
    metrics: ServiceMetrics,
    store: SpanStore,
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="stock", docs_url=None, redoc_url=None)
    app.add_middleware(ObservabilityMiddleware, service="stock", metrics=metrics, store=store)
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(None, store))

    @app.post("/stock")
    async def stock(body: StockBody) -> dict[str, Any]:
        try:
            answer = await check_stock(body.sku, current_deadline())
        except InjectedFailure as error:
            raise HTTPException(status_code=500, detail="simulated failure") from error
        except UnknownSku as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except DeadlineExceeded as error:
            raise HTTPException(status_code=504, detail="deadline exceeded") from error
        return {"sku": answer.sku, "inStock": answer.in_stock, "quantity": answer.quantity}
```

This route has the same parts as the gateway's: a Pydantic body, a call to the use case, and a mapping of errors to statuses. The use case raises domain errors and the handler maps each to an HTTP status: `InjectedFailure` to 500, `UnknownSku` to 404, `DeadlineExceeded` to 504 (the caller's time ran out). `current_deadline()` reads the deadline the middleware stored for this request.

### The middleware, written as raw ASGI

```python
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        path = scope["path"] if scope["type"] == "http" else ""
        if scope["type"] != "http" or path.startswith(_QUIET_PREFIXES):
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        parent = parse_traceparent(headers.get(TRACEPARENT))
        if parent is None:
            sampled = FORCE_HEADER.lower() in headers or self._rand() < self._sample_rate
            context = new_trace(sampled)
            parent_id = None
        else:
            context = child_of(parent)
            parent_id = parent.span_id
        now = self._clock()
        deadline = Deadline.from_header(headers.get(DEADLINE_HEADER.lower()), now)
        route = path
        status = 500
        self._metrics.received.labels(route).inc()
        self._metrics.in_flight.inc()
        token = set_current(context)
        deadline_token = set_deadline(deadline)
        started = self._clock()

```

- A class with `__call__(self, scope, receive, send)` is an ASGI application wrapper (see ASGI in the intro). `scope["type"] != "http"` and paths starting with `/health`, `/metrics` or `/admin` pass straight through: probes and control routes are not business requests and must not distort the counters.
- It lowercases the header names (`scope["headers"]` is a list of byte pairs), parses `traceparent` (the trace the caller started) or starts a new one, and reads the `X-Deadline-Ms` header into a `Deadline`.
- `self._metrics.received.labels(route).inc()` counts the request; `in_flight.inc()` adds it to the gauge. These counters are how the lesson measures amplification.
- `set_current(context)` and `set_deadline(deadline)` store the trace and the deadline in context variables for this request (see `python.md`).

```python
        async def send_with_trace(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-trace-id", context.trace_id.encode()))
            await send(message)

        try:
            with (
                correlation_scope(context.trace_id),
                SpanRecorder(
                    self._store,
                    self._service,
                    f"{self._service} {route}",
                    context,
                    parent_id,
                    {"deadlineMs": None if deadline is None else int(deadline.remaining(now) * 1000)},
                ) as span,
            ):
                try:
                    await self._app(scope, receive, send_with_trace)
                finally:
                    span.status = str(status)
                    if context.sampled or status >= 500:
                        _log.info("request", extra=_fields(scope, status, self._clock() - started))
        finally:
            self._metrics.in_flight.dec()
            self._metrics.duration.labels(route, str(status)).observe(self._clock() - started)
            reset_deadline(deadline_token)
            reset_current(token)
```

`send_with_trace` wraps `send` so that when the response starts, the middleware records the status and adds an `x-trace-id` header. The `try`/`finally` blocks guarantee clean-up even when the request fails: the in-flight gauge goes down, the duration is recorded in a histogram, and the context variables are restored. A log line is written only for sampled traces and for 5xx answers, to keep the log small under load.

### Routers for shared routes

```python
def health_router(registry: HealthRegistry) -> APIRouter:
    """Routes ``/health/live`` (always 200) and ``/health/ready`` (503 while a dependency is down)."""
    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "up"}

    @router.get("/health/ready")
    async def ready(response: Response) -> dict[str, Any]:
        report = registry.run()
        if not report.ready:
            response.status_code = 503
        return {"status": "ready" if report.ready else "not ready", "checks": report.checks}

    return router
```

`APIRouter` groups routes defined once and added to every service. `/health/ready` runs the registered checks and changes the status to 503 when one fails.

### Lifespan and background tasks

See `asyncio.md`: `services/stock/src/stock/interfaces/bootstrap.py` defines a lifespan that starts a background task and, after `yield`, cancels it and closes the pool. `app.router.lifespan_context = lifespan` installs it.

### The whole chain in one process

```python
    def client(app: object, caller: str, target: str) -> HopClient:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        inner = httpx.AsyncClient(transport=transport, base_url=f"http://{target}")
        return HopClient(
            caller, target, f"http://{target}", metrics=metrics[caller], store=stores[caller], client=inner
        )
```

`httpx.ASGITransport(app=app)` lets an HTTP client call a FastAPI app directly in memory, with no network. `tests/system/chain.py` builds all four real apps this way, each calling the next through an in-memory transport, so the mechanisms of the lesson can be tested in one process (see `httpx.md`).

### Where the tests use FastAPI

- `services/gateway/tests/unit/interfaces/test_api_and_settings.py`, `services/search/tests/unit/interfaces/test_api_and_settings.py`, `services/pricing/tests/unit/interfaces/test_api_and_settings.py` and `services/stock/tests/unit/interfaces/test_api_and_settings.py` build each real app around fakes and call it with `TestClient`.
- `libs/chassis/tests/unit/test_tracing_observability_upstream.py` builds a small `FastAPI()` with the real middleware, the metrics route and the admin router.
- `tests/system/chain.py` builds all four real apps and connects them with `httpx.ASGITransport`; `tests/system/test_chain_in_process.py` drives that chain.

All are read line by line in `pytest.md`.

## Try it yourself

1. Call the real health route in memory:

   ```bash
   uv run python -c "
   import asyncio
   import httpx
   from fastapi import FastAPI
   from chassis.health import HealthRegistry
   from chassis.web import health_router
   app = FastAPI(); app.include_router(health_router(HealthRegistry()))
   async def main():
       async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://x') as c:
           r = await c.get('/health/ready')
           print(r.status_code, r.json())
   asyncio.run(main())"
   ```

   Expect `200 {'status': 'ready', 'checks': {}}`.

2. Run the in-process chain tests:

   ```bash
   uv run pytest tests/system/test_chain_in_process.py -q
   ```

   Expect `9 passed`: a request crossing all four apps under one trace id, amplification under the naive profile, a budget that stops retries, a breaker that opens, shedding, and more.

3. With the stack up, send one request and read the headers:

   ```bash
   curl -si -X POST localhost:43080/search -H 'content-type: application/json' -d '{"query":"widget"}'
   ```

   Expect `HTTP/1.1 200 OK`, an `x-trace-id:` header and a body like `{"query":"widget","sku":"sku-...","price":...,"inStock":true,"degraded":false,"stale":false}`.

4. Switch a service's profile through the admin route:

   ```bash
   curl -s -X POST localhost:43080/admin/profile -H 'content-type: application/json' -d '{"name":"full"}'; curl -s localhost:43080/admin/profile
   ```

   Expect `{"name":"full"}` twice. Set it back with `{"name":"naive"}`.

## Mistakes people make

- **Blocking inside `async def`.** A synchronous database call in an async handler freezes the whole server. Use `def` for blocking work.
- **Returning the wrong status for validation failures** by hand. Let the model validate; FastAPI answers 422.
- **Leaking internals in errors.** `detail=str(error)` is fine for a domain message; never return a stack trace.
- **Business rules inside handlers.** Keep handlers thin: translate HTTP to a call and the result back, as the code here does.
- **Leaving interactive docs on in production** when you did not intend to publish the API shape. This repository turns them off.

## Check yourself

1. What makes FastAPI return 422 by itself?
   Answer: the request body or parameters fail validation against the type hints or the pydantic model.
2. What is the difference between a `def` and an `async def` handler?
   Answer: `def` runs in a worker thread and may block; `async def` runs on the event loop and must not block.
3. What is a middleware?
   Answer: a wrapper around the application that sees every request and response.
4. How do you answer with a 404?
   Answer: `raise HTTPException(status_code=404, detail=...)`.
5. What is ASGI?
   Answer: the standard interface between a Python web server and a Python web application (`scope`, `receive`, `send`).

## Further reading

- FastAPI documentation (this repository pins FastAPI 0.162, Starlette 1.10): https://fastapi.tiangolo.com/
- Tutorial, first steps: https://fastapi.tiangolo.com/tutorial/first-steps/
- Handling errors: https://fastapi.tiangolo.com/tutorial/handling-errors/
- Starlette middleware: https://starlette.dev/middleware/
- ASGI specification: https://asgi.readthedocs.io/
