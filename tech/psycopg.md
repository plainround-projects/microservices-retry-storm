# psycopg 3 and its async connection pool

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

`psycopg` is a Python library that talks to PostgreSQL. You give it a connection string; it opens a connection, sends SQL text with values attached, and turns the answers into Python values. The `[binary]` extra installs a compiled speed-up, and `psycopg_pool` is a companion package that keeps a **pool**: a small set of already-open connections that code borrows and returns.

## Why this repository uses it

The stock service is asynchronous (see `asyncio.md`), so its database driver must not block the event loop. psycopg 3 has an async API (`AsyncConnection`) and an async pool (`AsyncConnectionPool`) that mirror the synchronous ones (the same classes and methods, with `Async` in the name): you `await` the calls that wait and use `async with` for connections.

## Where it appears

| Path | What it does with psycopg |
| --- | --- |
| `libs/chassis/src/chassis/postgres.py` | Creates the async pool, waits for the database to come up, readiness check |
| `services/stock/src/stock/infrastructure/postgres.py` | The repository: one query |
| `services/stock/tests/integration/` | The adapter against the real database |
| `pyproject.toml`, `libs/chassis/pyproject.toml` | The dependency `psycopg[binary,pool]>=3.2,<4` |

## How it works underneath

- `psycopg.connect(url)` opens a TCP connection and logs in. A **cursor** runs a statement. `connection.execute(sql, params)` is a shortcut that makes a cursor, runs the statement and returns the cursor.
- `%s` in the SQL text marks a value. The values are sent in a separate list, never pasted in, which prevents SQL injection.
- A **row factory** decides what a row looks like. The default is a tuple. `dict_row` makes a dictionary keyed by column name.
- A **transaction** is a group of statements that succeed or fail together. With the default settings, psycopg starts a transaction on the first statement. Leaving a `with connection:` block commits if no error happened and rolls back if one did. With `autocommit=True` every statement commits by itself.
- A **pool** opens `min_size` connections at start and grows up to `max_size`. `with pool.connection() as conn:` borrows one and returns it at the end, committing or rolling back as above. If every connection is busy and none frees up within `timeout` seconds, the caller gets a `PoolTimeout`.

The async API differs from the sync one in three ways: `await` on calls that talk to the server (`await connection.execute(...)`, `await cursor.fetchone()`), `async with pool.connection() as connection:` to borrow a connection, and `await pool.open()` and `await pool.close()` for the pool itself.

## Walkthrough of this repository's use

### The pool factory

```python
Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
_SESSION_OPTIONS = "-c statement_timeout=3000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000"


async def create_pool(
    dsn: str, *, name: str, min_size: int = 2, max_size: int = 16, wait_seconds: float = 60.0
) -> Pool:
    """Open a pool of connections that return rows as dictionaries, waiting for the database with backoff."""
    deadline = time.monotonic() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=2.0,
            name=name,
            open=False,
            check=AsyncConnectionPool.check_connection,
            max_idle=120.0,
            kwargs={"row_factory": dict_row, "connect_timeout": 5, "options": _SESSION_OPTIONS},
        )
        try:
            await pool.open(wait=True, timeout=3.0)
            return cast(Pool, pool)
        except PoolTimeout:
            await pool.close()
            if time.monotonic() >= deadline:
                raise
            _log.warning("waiting for database", extra={"database": name, "attempt": attempt})
            await asyncio.sleep(min(2.0, 0.2 * 2**attempt))
```

- `Pool = AsyncConnectionPool[AsyncConnection[DictRow]]` gives a short name to a long type: a pool whose connections return dictionary rows.
- The function retries while the database container starts. `deadline` is when it gives up.
- `AsyncConnectionPool(dsn, min_size=2, max_size=16, timeout=2.0, name=name, open=False, ...)`: keep 2 to 16 connections, wait at most 2 seconds for a free one (a request that cannot get a connection fails fast instead of queueing), name the pool for logs, do not open yet.
- `check=AsyncConnectionPool.check_connection` tests a connection before handing it out and replaces a dead one. `max_idle=120.0` closes an unused connection after two minutes.
- `kwargs={...}` are passed to each `connect`: `row_factory: dict_row` (rows are dictionaries), `connect_timeout: 5` seconds, and `options` with the server-side limits (see `postgresql.md`).
- `await pool.open(wait=True, timeout=3.0)` opens and waits for `min_size` connections. On `PoolTimeout` the pool is closed, a warning is logged, and `await asyncio.sleep(...)` pauses with growing delays before another try. `cast(Pool, pool)` tells `mypy` the type; it does nothing at run time.

### The repository

```python
class PostgresStockRepository:
    """Reads ``stock_levels`` through the async pool."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def quantity(self, sku: str) -> int | None:
        """Units available, or ``None`` for an unknown sku."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute("SELECT quantity FROM stock_levels WHERE sku = %s", (sku,))
            row = await cursor.fetchone()
        return None if row is None else int(row["quantity"])
```

`async with self._pool.connection() as connection:` borrows a connection and returns it at the end of the block, committing if there was no error. `connection.execute("... WHERE sku = %s", (sku,))` sends the value in a tuple (the comma makes it a one-element tuple) separately from the SQL. `await cursor.fetchone()` returns a dictionary or `None`; the code turns `None` into "unknown SKU".

### Readiness without queuing behind the load

```python
async def check_pool(pool: Pool) -> None:
    """Readiness check: run ``SELECT 1`` on a pooled connection."""
    async with pool.connection() as connection:
        await connection.execute("SELECT 1")
```

`check_pool` is the direct check. The stock service does not call it on every probe; it runs the same `SELECT 1` in a background loop (see `asyncio.md`) and the probe reads the stored result.

### Fixtures for the integration test

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

`@pytest_asyncio.fixture` is the async version of a pytest fixture (see `pytest-asyncio.md`): code before `yield` sets up (here: `await create_pool(...)`), code after it cleans up (`await pool.close()`).

### Where the integration test uses psycopg

`services/stock/tests/integration/conftest.py` builds the async pool as the application role, and `test_postgres_stock.py` reads the seeded stock and checks that an `UPDATE` raises `errors.InsufficientPrivilege` (`from psycopg import errors`). Both are read line by line in `pytest.md`.

## Try it yourself

These need the stack (`make up`) and the passwords from `.env`.

1. Open the pool and read a row:

   ```bash
   set -a; . ./.env; set +a
   uv run python - <<'PY'
   import asyncio, os
   from chassis.postgres import create_pool

   async def main():
       url = f"postgresql://stock_app:{os.environ['STOCK_APP_PASSWORD']}@127.0.0.1:{os.environ['STOCK_DB_PORT']}/stock"
       pool = await create_pool(url, name="try", wait_seconds=5)
       async with pool.connection() as c:
           cur = await c.execute("SELECT sku, quantity FROM stock_levels WHERE sku = %s", ("sku-005",))
           print(await cur.fetchone())
           cur = await c.execute("SHOW statement_timeout")
           print(await cur.fetchone())
       await pool.close()

   asyncio.run(main())
   PY
   ```

   Expect `{'sku': 'sku-005', 'quantity': 6}` and `{'statement_timeout': '3s'}`.

2. Run the adapter's integration tests:

   ```bash
   cd services/stock && uv run pytest tests/integration -m integration -q
   ```

   Expect `2 passed`.

3. Ask the pool for a database that is not there:

   ```bash
   uv run python -c "import asyncio; from chassis.postgres import create_pool; asyncio.run(create_pool('postgresql://x:y@127.0.0.1:1/z', name='t', wait_seconds=1))"
   ```

   Expect a traceback ending in `psycopg_pool.PoolTimeout: pool initialization incomplete after 3.0 sec`.

## Mistakes people make

- **Pasting values into the SQL string** with f-strings. Use `%s` and a tuple.
- **Forgetting that a connection stays in a transaction.** Without the `with` block, or `autocommit`, changes are never committed.
- **Not closing the pool** at shutdown. The server keeps the sessions until they time out.
- **Holding a pooled connection during a slow call** to another service. The pool runs dry. Borrow, do the SQL, return.
- **Reading a one-element tuple as a value.** `fetchone()` returns a row (here a dictionary), or `None` when nothing matched.

## Check yourself

1. What does `with pool.connection() as conn:` do at the end of the block?
   Answer: commits when there was no error, rolls back when there was, and returns the connection to the pool.
2. Why `%s` and not an f-string?
   Answer: values travel separately from the SQL, so they cannot change its meaning.
3. What does `dict_row` change?
   Answer: rows come back as dictionaries keyed by column name.
4. What is a `PoolTimeout`?
   Answer: no connection became free within the wait time.
5. What does `fetchone()` return when no row matches?
   Answer: `None`.

## Further reading

- psycopg 3 documentation (this repository pins psycopg 3.3 and psycopg_pool 3.3): https://www.psycopg.org/psycopg3/docs/
- Connection pools: https://www.psycopg.org/psycopg3/docs/advanced/pool.html
- Row factories: https://www.psycopg.org/psycopg3/docs/advanced/rows.html
- Transactions: https://www.psycopg.org/psycopg3/docs/basic/transactions.html
