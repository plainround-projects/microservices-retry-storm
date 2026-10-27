# PostgreSQL

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

PostgreSQL (often "Postgres") is a relational database **server**: a program that runs all the time, keeps tables on disk, and answers SQL sent to it over the network (see `sql.md`). A **client**, such as the `psql` command-line program or a Python library, connects, sends SQL and reads the answers.

## Why this repository uses it

The bottom of the chain, the stock store, is backed by a real PostgreSQL database. The lesson's storm is made of requests waiting for the stock store; having an actual database behind it, with a real connection pool and real timeouts, keeps the experiment honest. The database holds twenty rows and is read-only for the service.

## Where it appears

| Path | What it does with PostgreSQL |
| --- | --- |
| `docker-compose.yml` | One server from the image `postgres:18` (`stock-db`) and one init job (`stock-db-init`) |
| `.env.example` | Port and passwords (throwaway values) |
| `services/stock/sql/` | Schema, seed, queries (see `sql.md`) |
| `libs/chassis/src/chassis/postgres.py` | The async pool and the session limits (see `psycopg.md`) |
| `services/stock/src/stock/infrastructure/postgres.py` | The one query |

## How it works underneath

- The server listens on a **port** (5432 by default). A client opens a TCP connection to it and logs in as a **role** with a password. A role is a name with rights. A role that may log in is what other systems call a user.
- One server holds many **databases**. Each database holds **schemas**, and each schema holds tables. Tables here live in the default schema, `public`.
- Rights are **privileges** granted to roles per table or column: `SELECT`, `INSERT`, `UPDATE`, `DELETE`. A role that owns a table may do anything to it. **Least privilege** means the program that serves requests connects as a role that owns nothing and has only the rights it needs.
- Every connection costs the server memory, so programs keep a small **pool** of connections and reuse them (see `psycopg.md`).
- The server writes every change to a log on disk (the write-ahead log) before it tells the client "done". That is why committed data survives a crash.
- `pg_isready` is a small tool that asks a server "are you accepting connections?". It is used in health checks.
- The server enforces limits when told to: `statement_timeout` cancels a statement that runs too long, `lock_timeout` gives up waiting for a lock, `idle_in_transaction_session_timeout` ends a session that opened a transaction and went quiet.

## Walkthrough of this repository's use

### The server and its init job

```yaml
  stock-db:
    image: postgres:18
    environment:
      POSTGRES_USER: stock_owner
      POSTGRES_PASSWORD: ${STOCK_OWNER_PASSWORD:?run make env}
      POSTGRES_DB: stock
    ports: ["127.0.0.1:${STOCK_DB_PORT:-43432}:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U stock_owner -d stock"]
      interval: 2s
      timeout: 3s
      retries: 40

  stock-db-init:
    image: postgres:18
    entrypoint: ["/scripts/apply-sql.sh", "/sql"]
    environment:
      PGHOST: stock-db
      PGUSER: stock_owner
      PGPASSWORD: ${STOCK_OWNER_PASSWORD:?run make env}
      PGDATABASE: stock
      APP_PASSWORD: ${STOCK_APP_PASSWORD:?run make env}
    volumes:
      - ./scripts/apply-sql.sh:/scripts/apply-sql.sh:ro
      - ./services/stock/sql:/sql:ro
    depends_on:
      stock-db: { condition: service_healthy }
    restart: "no"
```

- `image: postgres:18` is the official image. On first start it creates a cluster, a role `stock_owner` (a superuser, because the image makes the `POSTGRES_USER` one) and an empty database `stock`.
- `${STOCK_OWNER_PASSWORD:?run make env}` reads the password from `.env` and stops with the hint if it is missing (see `docker-compose.md`).
- `ports: ["127.0.0.1:${STOCK_DB_PORT:-43432}:5432"]` publishes the database on the laptop only.
- The `healthcheck` runs `pg_isready` every 2 seconds, up to 40 failures.
- `stock-db-init` uses the same image only for its `psql` client. It reads the standard `PG*` variables, mounts the SQL folder read-only and runs `scripts/apply-sql.sh` once (`restart: "no"`). The stock service waits for it with `service_completed_successfully`.

### Owner and application roles

The owner creates tables and runs the SQL. `001_schema.sql` creates `stock_app`, and the stock service connects as it: `postgresql://stock_app:<password>@stock-db:5432/stock` (scheme, user, password, host, port, database). Its only right is `SELECT` on `stock_levels` (see `sql.md`).

### Limits on every connection

```python
Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
_SESSION_OPTIONS = "-c statement_timeout=3000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000"
```

Every pooled connection starts with: statements stop after 3 seconds (`statement_timeout`), waiting for a lock stops after 1 second, and a transaction left idle is ended after 10 seconds. These server-side limits stop a stuck query from holding a connection for ever. Note that the experiment's slowdown is injected in the service (`LATENCY_MS`), not in PostgreSQL: the database itself answers a one-row lookup in a millisecond.

### How the service waits for the database

```python
async def build_app(settings: Settings) -> FastAPI:
    """Open the pool, wire the use case, and close the pool when the app shuts down."""
    pool = await create_pool(settings.database_url, name="stock")
    faults = FaultState(Faults(settings.latency_ms, settings.fail_rate))
    metrics = ServiceMetrics("stock")
    health = HealthRegistry()
    state = {"database": "ok"}

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
```

The service opens the pool (retrying while the database starts), and a background task checks `SELECT 1` every two seconds and stores the result. `/health/ready` reads the stored result, so thousands of probes never queue behind the load for a database connection.

## Try it yourself

These need the stack (`make up`).

1. Ask the server if it is ready:

   ```bash
   docker compose exec -T stock-db pg_isready -U stock_owner -d stock
   ```

   Expect `/var/run/postgresql:5432 - accepting connections`.

2. List the roles:

   ```bash
   docker compose exec -T stock-db psql -U stock_owner -d stock -c "\du"
   ```

   Expect `stock_owner` with `Superuser` among its attributes and `stock_app` with none.

3. Look at the table:

   ```bash
   docker compose exec -T stock-db psql -U stock_owner -d stock -c "\d stock_levels"
   ```

   Expect two columns, `sku` (text, primary key) and `quantity` (integer, not null), and a check constraint `quantity >= 0`.

4. Count the rows:

   ```bash
   docker compose exec -T stock-db psql -U stock_owner -d stock -c "SELECT count(*) FROM stock_levels"
   ```

   Expect `20`.

## Mistakes people make

- **Connecting the application as the owner or as a superuser.** A bug or an injection then has the power to drop tables.
- **Treating "the port answers" as "ready".** The server can accept connections while still recovering after start-up.
- **Storing data inside the container only.** When the container is removed, the data goes with it (here that is intended; for real data use a named volume and backups).
- **No timeouts.** A stuck query holds a connection for ever and the pool runs dry.
- **Opening a new connection for every request.** Use a pool.

## Check yourself

1. What is a role?
   Answer: a name with rights; one that can log in acts as a database user.
2. Why does the application not connect as the owner?
   Answer: least privilege; if the app is compromised or buggy it can only do what its grants allow.
3. What does `pg_isready` tell you?
   Answer: whether the server accepts connections.
4. What does `statement_timeout` do?
   Answer: cancels any statement that runs longer than the limit.
5. Why use a connection pool?
   Answer: connections are expensive; a pool reuses a few instead of opening one per request.

## Further reading

- PostgreSQL 18 documentation (the version this repository runs): https://www.postgresql.org/docs/18/
- Privileges: https://www.postgresql.org/docs/18/ddl-priv.html
- Server configuration, client connection defaults (timeouts): https://www.postgresql.org/docs/18/runtime-config-client.html
- The official Docker image: https://hub.docker.com/_/postgres
