# pytest and pytest-cov

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
