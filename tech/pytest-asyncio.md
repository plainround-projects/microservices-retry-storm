# pytest-asyncio

## What it is

pytest-asyncio is a plugin that lets pytest run tests written as `async def` (see `asyncio.md`). pytest by itself only calls ordinary functions; an `async def` function returns a coroutine that nothing runs. The plugin starts an event loop, runs the coroutine to the end, and reports the result as usual.

## Why this repository uses it

The services, the policy loops and the hop client are `async`, so their tests are `async` too. Running them in a real event loop is what lets the tests check timeouts, cancellation and concurrent requests, not just return values.

## Where it appears

| Path | What it does |
| --- | --- |
| `pyproject.toml` (root, `dev` group) | The dependency `pytest-asyncio>=0.24` (version 1.4.1 in the lock) |
| `pyproject.toml` files | `asyncio_mode = "auto"` in each `[tool.pytest.ini_options]` that has async tests |
| `libs/resilience/tests/unit/test_retry_loops.py`, `libs/chassis/tests/unit/`, `services/*/tests/unit/` | `async def` tests |
| `services/stock/tests/integration/conftest.py` | An async fixture built with `@pytest_asyncio.fixture` |
| `tests/system/test_chain_in_process.py` | Async tests that run the whole chain |

## How it works underneath

- The plugin has **modes**. In `strict` mode (the default), you mark each async test with `@pytest.mark.asyncio` and each async fixture with `@pytest_asyncio.fixture`. In `auto` mode (`asyncio_mode = "auto"` in the configuration), every `async def` test is run as an asyncio test without a marker, and async fixtures are recognized automatically.
- By default each test gets its own fresh event loop, so tests cannot leave tasks running for the next test.
- An **async fixture** is a fixture that is an `async def` generator: set-up code with `await`, then `yield value`, then clean-up code with `await`. The test receives the yielded value.
- A test that never reaches an `await` still works; a test that awaits something that never finishes hangs, so tests should use timeouts.

## Walkthrough of this repository's use

### The mode

```toml

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers --import-mode=importlib"
pythonpath = ["."]
asyncio_mode = "auto"
markers = [
    "e2e: the chain across the running containers (make up first)",
    "experiment: a measured experiment from the lesson (make up first, minutes)",
]
```

`asyncio_mode = "auto"` is why the tests in this repository are written as plain `async def test_...` with no decorator.

### An async test

```python
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

`async def test_...` is run by the plugin on an event loop. Inside, `await run_naive(attempt)` runs the code under test to the end. `pytest.raises` works in async tests exactly as in sync ones. The attempt functions are `async def` too (they are what `run_naive` awaits).

### An async fixture

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

`@pytest_asyncio.fixture` marks an async generator as a fixture. `await create_pool(...)` sets it up; the pool goes to the test through `yield`; `await pool.close()` runs after the test. Because the pool is created inside the test's own event loop and closed there, it is never used from another loop.

### Concurrency inside a test

```python
    answers = await asyncio.gather(*[chain.gateway.post("/search", json={"query": f"q{i}"}) for i in range(12)])
```

`asyncio.gather(*[...])` sends twelve requests at the same moment through the in-memory chain and collects the answers; the test then asserts on the counters.

## Try it yourself

1. Run an async test file:

   ```bash
   cd libs/resilience && uv run pytest tests/unit/test_retry_loops.py -q
   ```

   Expect `12 passed`.

2. See the plugin at work by listing its version and mode in the pytest header:

   ```bash
   cd libs/resilience && uv run pytest tests/unit/test_retry_loops.py -q -k naive -v 2>&1 | head -6
   ```

   Expect a header with `asyncio: mode=Mode.AUTO` among the plugin lines.

3. Turn the mode off for one run and see why it matters:

   ```bash
   cd libs/resilience && uv run pytest tests/unit/test_retry_loops.py -q -o asyncio_mode=strict 2>&1 | tail -3
   ```

   Expect `11 failed, 1 passed`: without `auto`, each async test would need `@pytest.mark.asyncio`, so pytest cannot run them.

## Mistakes people make

- **Writing an `async def` test without the plugin configured.** pytest then skips it with a warning, or it "passes" without running.
- **Mixing a sync fixture that uses `asyncio.run` with an async test.** Running a loop inside a running loop is an error.
- **Sharing an async resource across event loops.** A pool created in one test's loop cannot be used in another's; build it in a fixture with the right scope.
- **Tasks that outlive the test.** Cancel them in the clean-up half of the fixture.

## Check yourself

1. What does the plugin add to pytest?
   Answer: the ability to run `async def` tests and async fixtures on an event loop.
2. What does `asyncio_mode = "auto"` change?
   Answer: async tests need no marker and async fixtures are recognized automatically.
3. How is an async fixture written?
   Answer: as an `async def` generator that does set-up, yields the value, then does clean-up.
4. Why does each test get its own event loop by default?
   Answer: so one test's leftover tasks cannot affect another.
5. What happens to an `async def` test if the plugin is missing?
   Answer: pytest does not run its body; it is skipped or warned about.

## Further reading

- pytest-asyncio documentation (this repository pins pytest-asyncio 1.4): https://pytest-asyncio.readthedocs.io/en/stable/
- Modes: https://pytest-asyncio.readthedocs.io/en/stable/reference/configuration.html
- Fixtures: https://pytest-asyncio.readthedocs.io/en/stable/reference/fixtures/index.html
