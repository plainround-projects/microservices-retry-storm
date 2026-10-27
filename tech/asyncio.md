# asyncio, async and await

## What it is

`asyncio` is Python's built-in way to handle many waiting jobs in one thread. A web service spends most of its time waiting: for a network answer, for a database. While one request waits, the same thread can work on another. An **async function** (`async def`) is a function that is allowed to pause at certain points. A **coroutine** is the object you get by calling it; it does nothing until something runs it.

## Why this repository uses it

The lesson is about requests that wait: a gateway waits for search, search waits for pricing, pricing waits for stock. When the bottom is slow, thousands of requests are waiting at once, and the experiment has to hold thousands of them in one process without a thread for each. Async code makes that cheap, and the timeouts that stop the storm are `asyncio.timeout` blocks.

## Where it appears

| Path | What it does with asyncio |
| --- | --- |
| `libs/resilience/src/resilience/retry.py` | The retry loops are coroutines; the pause between attempts is `await sleep(...)` |
| `libs/chassis/src/chassis/upstream.py` | `HopClient.call`: one attempt, with `asyncio.timeout` |
| `libs/chassis/src/chassis/observability.py` | The middleware is an async ASGI callable |
| `services/stock/src/stock/application/check_stock.py` | A semaphore for capacity, `asyncio.timeout` for the deadline |
| `services/stock/src/stock/interfaces/bootstrap.py` | A background task and an async lifespan |
| `services/*/src/*/interfaces/main.py` | `asyncio.run` starts the server |
| `tests/system/`, `scripts/` | The load generator and the experiments (`create_task`, `gather`, `sleep`) |

## How it works underneath

- The **event loop** is a loop that keeps a list of coroutines and runs each one until it reaches an `await`. `await something` means "pause me until `something` is done, and let others run meanwhile". When the awaited thing finishes, the loop resumes the coroutine.
- A coroutine only pauses at `await`. If it runs a long calculation or a blocking call (`time.sleep`, a synchronous database call) the whole loop is stuck, and every other request waits. This is why async code uses `await asyncio.sleep(...)` and async libraries.
- `asyncio.run(main())` starts a loop and runs `main()` to completion.
- `asyncio.create_task(coro)` starts a coroutine running in the background and returns a **task**; `task.cancel()` asks it to stop by raising `CancelledError` inside it. `asyncio.gather(a, b, c)` runs several coroutines at the same time and collects their results.
- `async with` and `async for` are the versions of `with` and `for` whose set-up and clean-up can pause.
- `asyncio.timeout(seconds)` is a block that cancels what is inside it, and raises `TimeoutError`, when the time runs out. Because cancellation is delivered at an `await`, a timeout can only interrupt code that is waiting, which is why work must be written with `await`.
- `asyncio.Semaphore(n)` lets at most `n` coroutines inside a section at once; others wait their turn.
- A **thread** can also run code in parallel, but a thread costs memory and can be preempted anywhere; coroutines switch only at `await`, so shared data is safer.

## Walkthrough of this repository's use

### A timeout around one network call

```python
            try:
                async with asyncio.timeout(timeout):
                    response = await self._client.post(path, json=body, headers=headers)
            except TimeoutError as error:
                span.status = "timeout"
                raise AttemptFailed("timeout") from error
            except httpx.TransportError as error:
                span.status = "connect-error"
                raise AttemptFailed(type(error).__name__) from error
```

`async with asyncio.timeout(timeout):` wraps the `await self._client.post(...)`. If the other service does not answer in time, the call is cancelled and `TimeoutError` is raised; the code turns it into the policy's own `AttemptFailed("timeout")`. `raise ... from error` keeps the cause.

### A capacity limit that also honors a deadline

```python
    async def __call__(self, sku: str, deadline: Deadline | None) -> StockAnswer:
        """Return the stock of ``sku``; raise ``InjectedFailure``, ``UnknownSku`` or ``DeadlineExceeded``."""
        if deadline is None:
            return await self._work(sku)
        remaining = deadline.remaining(self._clock())
        if remaining <= 0:
            raise DeadlineExceeded
        try:
            async with asyncio.timeout(remaining):
                return await self._work(sku)
        except TimeoutError as error:
            raise DeadlineExceeded from error

    async def _work(self, sku: str) -> StockAnswer:
        async with self._capacity:
            faults = self._faults()
            await self._sleep(faults.latency_ms / 1000.0)
            if self._rand() < faults.fail_rate:
                raise InjectedFailure
            quantity = await self._repository.quantity(sku)
        if quantity is None:
            raise UnknownSku(sku)
        return StockAnswer(sku, quantity)
```

- `self._capacity` is an `asyncio.Semaphore(16)` created in `bootstrap.py`: the stock store can do 16 things at once, like a database with a limited number of connections. `async with self._capacity:` waits for a free slot.
- A request that arrives **with** a deadline runs inside `asyncio.timeout(remaining)`, covering both the wait for a slot and the work. When the deadline passes, the request stops and frees its place. That is why abandoned requests stop costing anything.
- A request **without** a deadline (the naive chain) is carried through to the end even if its caller left long ago. That difference is the whole mechanism by which a backlog outlives the slowdown that built it.
- `await self._sleep(faults.latency_ms / 1000.0)` simulates the slow store; `sleep` is a parameter, so tests can replace it.

### Retry loops that wait without blocking

```python
async def run_budgeted[T](
    attempt: Callable[[float], Awaitable[T]],
    *,
    deadline: Deadline,
    budget: RetryBudget,
    backoff: Backoff,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float],
    max_attempts: int = 3,
    hop_timeout: float = 1.8,
    margin: float = 0.05,
    min_useful: float = 0.1,
    on_retry: Callable[[int, str], None] | None = None,
) -> T:
    """Try, and retry only if all of these hold: the error is retryable, the budget allows it, enough time is left.

    The pause between attempts is exponential with full jitter (or the server's ``Retry-After`` if it is longer),
    and the pause must itself fit in the remaining time. No attempt gets a timeout beyond what is left of the deadline.
    """
    budget.record_request()
    last = AttemptFailed("no attempt made")
    for number in range(1, max_attempts + 1):
        timeout = deadline.attempt_timeout(clock(), hop_timeout, margin)
```

`sleep: Callable[[float], Awaitable[None]] = asyncio.sleep` is the default pause, a coroutine function. The loop awaits it between attempts (`await sleep(pause)`, line 88): while this request pauses, the loop serves other requests.

### Starting the server and a background task

```python
async def _serve(settings: Settings) -> None:
    app = build_app(settings)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_config=None, timeout_graceful_shutdown=10, backlog=4096
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    """Read the environment, build the application and serve until SIGTERM, then drain in-flight requests."""
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        configure_logging("gateway")
        logging.getLogger("gateway").error(str(error))
        sys.exit(2)
    configure_logging("gateway", settings.log_level)
    asyncio.run(_serve(settings))
```

`async def _serve` builds the app and awaits `uvicorn.Server(config).serve()`; `asyncio.run(_serve(settings))` starts the event loop and runs it until the server stops (see `uvicorn.md`).

```python
    async def refresh_database_check() -> None:
        while True:
            try:
                async with pool.connection() as connection:
                    await connection.execute("SELECT 1")
                state["database"] = "ok"
            except Exception:
                state["database"] = "failed"
            await asyncio.sleep(2.0)

    def database_ready() -> None:
        if state["database"] != "ok":
            raise ConnectionError("database")

    health.add("database", database_ready)
    use_case = CheckStock(
        PostgresStockRepository(pool),
        faults.get,
        asyncio.Semaphore(settings.capacity),
        clock=time.monotonic,
        rand=random.random,
    )
    app = create_app(use_case, faults, health, metrics, SpanStore())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        checker = asyncio.create_task(refresh_database_check())
        yield
        checker.cancel()
        await pool.close()

    app.router.lifespan_context = lifespan
    return app
```

`asyncio.create_task(refresh_database_check())` starts a loop that runs in the background for the life of the application: every two seconds it checks the database and stores `ok` or `failed` in a dictionary. The readiness route reads that stored value instead of touching the database on every probe. `checker.cancel()` stops it at shutdown. `@asynccontextmanager` turns an async generator into an async context manager: code before `yield` runs at start-up, code after it at shutdown (see `fastapi.md`).

### Running many things at once in tests

```python
    answers = await asyncio.gather(*[chain.gateway.post("/search", json={"query": f"q{i}"}) for i in range(12)])
```

`asyncio.gather(*[chain.gateway.post(...) for i in range(12)])` sends twelve requests concurrently and waits for all the answers.

## Try it yourself

1. Run the policy tests, which are async functions:

   ```bash
   cd libs/resilience && uv run pytest tests/unit/test_retry_loops.py -q
   ```

   Expect all tests to pass quickly, even though they simulate backoff pauses (the clock and `sleep` are fakes).

2. Run the whole chain in one process, with no Docker:

   ```bash
   uv run pytest tests/system/test_chain_in_process.py -q
   ```

   Expect `passed` for every test; they send requests through the four real applications over in-memory transports (see `httpx.md`).

3. See a timeout cancel a wait:

   ```bash
   uv run python -c "
   import asyncio
   async def main():
       try:
           async with asyncio.timeout(0.1):
               await asyncio.sleep(5)
       except TimeoutError:
           print('timed out after 0.1 s, not 5 s')
   asyncio.run(main())"
   ```

   Expect `timed out after 0.1 s, not 5 s`, printed almost at once.

4. See concurrency: three one-second waits take one second, not three.

   ```bash
   uv run python -c "
   import asyncio, time
   async def main():
       t = time.monotonic()
       await asyncio.gather(asyncio.sleep(1), asyncio.sleep(1), asyncio.sleep(1))
       print(round(time.monotonic() - t, 1))
   asyncio.run(main())"
   ```

   Expect `1.0`.

## Mistakes people make

- **Blocking inside `async def`.** `time.sleep(1)`, `requests.get(...)` or a plain database driver freezes every request. Use the async version.
- **Forgetting `await`.** Calling `fetch()` without `await` creates a coroutine and runs nothing (Python warns "coroutine was never awaited").
- **Swallowing `CancelledError`.** Catching every exception can hide a cancellation and leave a task running.
- **Starting tasks and losing them.** Keep a reference to a task from `create_task`, or it may be collected.
- **Unbounded concurrency.** Starting a hundred thousand tasks at once can exhaust sockets or memory; limit with a semaphore.
- **Mixing event loops** by calling `asyncio.run` inside code that is already running in a loop.

## Check yourself

1. What happens at an `await`?
   Answer: the coroutine pauses and the event loop runs other work until the awaited thing is ready.
2. Why is `time.sleep(1)` a bug inside `async def`?
   Answer: it blocks the whole thread, so no other request makes progress; use `await asyncio.sleep(1)`.
3. What does `asyncio.timeout(2)` do when two seconds pass?
   Answer: cancels the code inside the block and raises `TimeoutError`.
4. What does a semaphore limit?
   Answer: how many coroutines can be inside a section at the same time.
5. What does calling an `async def` function without `await` give you?
   Answer: a coroutine object that has not run.

## Further reading

- asyncio documentation, Python 3.12: https://docs.python.org/3.12/library/asyncio.html
- Coroutines and tasks: https://docs.python.org/3.12/library/asyncio-task.html
- `asyncio.timeout`: https://docs.python.org/3.12/library/asyncio-task.html#asyncio.timeout
- Synchronization primitives (`Semaphore`): https://docs.python.org/3.12/library/asyncio-sync.html
