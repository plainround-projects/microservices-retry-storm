# Docker Compose

## What it is

Docker Compose is a tool, built into the `docker` command as `docker compose`, that starts a group of containers from one YAML file (see `yaml.md`). The file lists the **services** (each service becomes one or more containers), how to build or pull their images, their settings, and how they depend on each other.

## Why this repository uses it

One file starts the whole chain, from the database up to the gateway, in the right order, and the experiments drive individual containers through the same file: restart all four, stop the stock store, recreate search with a different concurrency limit.

## Where it appears

| Path | What it does |
| --- | --- |
| `docker-compose.yml` | The whole system |
| `.env.example` (copied to `.env` by `make up`) | Ports, passwords, and the knobs the experiments turn |
| `Makefile` | `up`, `down`, `db`, `queries`, `logs` wrap `docker compose` |
| `tests/system/harness.py`, `experiments.py` | `compose(...)`: restart, stop, start, recreate |
| `.github/workflows/ci.yml` | `make up`, the tests, `make down` |

## How it works underneath

- Compose creates a private **network** for the project. Each service is reachable from the others by its service name, so a database service named `db` is reachable at the host name `db`, and a URL like `postgresql://user:password@db:5432/app` works inside the network. The project name (from `name:` or `COMPOSE_PROJECT_NAME`) prefixes every created resource, such as the container `myproject-web-1`.
- **Variables.** Compose reads a file named `.env` next to the compose file, and the shell environment, and substitutes `${NAME}` in the YAML. `${NAME:-default}` uses `default` when the variable is unset or empty. `${NAME:?message}` stops with the message when it is unset or empty.
- **Ordering.** `depends_on` with a `condition` makes a service wait: `service_healthy` waits until the other service's health check passes, and `service_completed_successfully` waits until a one-shot container has exited with code 0. Without a condition it only waits for the other container to start.
- `docker compose up -d --build --wait` builds images, starts everything in the background, and waits until every service with a health check is healthy. `docker compose down -v` stops and removes the containers, the network and the volumes.
- `docker compose exec <service> <command>` runs a command inside a running container; `run --rm <service>` starts a fresh one-off container for a service.
- Top-level keys starting with `x-` are ignored by Compose and are used to hold reusable pieces with YAML anchors.

## Walkthrough of this repository's use

### Header and project name

```yaml
# The chain: gateway -> search -> pricing -> stock -> stock-db. Everything is bound to 127.0.0.1; the password comes
# from .env. The admin and metrics routes of every service are published so the experiments can drive and read them.
name: ${COMPOSE_PROJECT_NAME:-storm}
```

The comment states the shape: `gateway -> search -> pricing -> stock -> stock-db`, everything bound to `127.0.0.1`, and the admin and metrics routes published on purpose so the experiments can use them. `name: ${COMPOSE_PROJECT_NAME:-storm}` sets the project name; containers are called `storm-gateway-1` and so on.

### The shared settings

The `x-service` block and `<<: *service` are explained in `yaml.md`; the settings in `docker.md`.

### The database and its init job

The database, its health check and the one-shot job that applies the SQL are explained in `postgresql.md`.

### The stock store

```yaml
  stock:
    <<: *service
    build: { context: ., args: { SERVICE: stock } }
    image: storm-stock:dev
    environment:
      DATABASE_URL: postgresql://stock_app:${STOCK_APP_PASSWORD:?run make env}@stock-db:5432/stock
      STOCK_CONCURRENCY: ${STOCK_CONCURRENCY:-16}
      LATENCY_MS: ${LATENCY_MS:-50}
      FAIL_RATE: ${FAIL_RATE:-0}
      LOG_LEVEL: ${LOG_LEVEL:-WARNING}
    ports: ["127.0.0.1:${STOCK_PORT:-43083}:8080"]
    depends_on:
      stock-db-init: { condition: service_completed_successfully }
```

- `build: { context: ., args: { SERVICE: stock } }` builds from the repository root with the build argument that selects the package (see `dockerfile.md`). `image: storm-stock:dev` names the result.
- The `environment` variables are the knobs of the experiments: `STOCK_CONCURRENCY` (16: how many stock checks run at once), `LATENCY_MS` (50) and `FAIL_RATE` (0) are the starting latency and failure rate that the admin route changes at run time. `LOG_LEVEL` defaults to `WARNING` to keep logs quiet under load.
- `depends_on: stock-db-init: { condition: service_completed_successfully }` waits for the SQL job.

### The rest of the chain

```yaml
  pricing:
    <<: *service
    build: { context: ., args: { SERVICE: pricing } }
    image: storm-pricing:dev
    environment:
      STOCK_URL: http://stock:8080
      LOG_LEVEL: ${LOG_LEVEL:-WARNING}
    ports: ["127.0.0.1:${PRICING_PORT:-43082}:8080"]
    depends_on:
      stock: { condition: service_healthy }

  search:
    <<: *service
    build: { context: ., args: { SERVICE: search } }
    image: storm-search:dev
    environment:
      PRICING_URL: http://pricing:8080
      SEARCH_CONCURRENCY_LIMIT: ${SEARCH_CONCURRENCY_LIMIT:-40}
      LOG_LEVEL: ${LOG_LEVEL:-WARNING}
    ports: ["127.0.0.1:${SEARCH_PORT:-43081}:8080"]
    depends_on:
      pricing: { condition: service_healthy }

  gateway:
    <<: *service
    build: { context: ., args: { SERVICE: gateway } }
    image: storm-gateway:dev
    environment:
      SEARCH_URL: http://search:8080
      TRACE_SAMPLE_RATE: ${TRACE_SAMPLE_RATE:-0}
      LOG_LEVEL: ${LOG_LEVEL:-WARNING}
    ports: ["127.0.0.1:${GATEWAY_PORT:-43080}:8080"]
    depends_on:
      search: { condition: service_healthy }
```

Each service gets the address of the next one by its **service name** on the project network (`STOCK_URL: http://stock:8080`, `PRICING_URL: http://pricing:8080`, `SEARCH_URL: http://search:8080`). `SEARCH_CONCURRENCY_LIMIT` (40) is the load-shedding limit; `TRACE_SAMPLE_RATE` (0) is the share of requests whose trace is recorded. Every service waits for the next one to be **healthy** before it starts (`depends_on: ... condition: service_healthy`): pricing waits for stock, search for pricing, gateway for search. The start order is therefore database, stock, pricing, search, gateway.

### How the pieces start

`make up` runs `docker compose --ansi never up -d --build --wait --wait-timeout 1200`. Compose builds four images, starts the database, runs the SQL, then starts the services from the bottom of the chain up. `--wait` returns when every service with a health check is healthy.

## Try it yourself

These need Docker running.

1. List the services:

   ```bash
   make env && docker compose config --services
   ```

   Expect six names: `stock-db`, `stock-db-init`, `stock`, `pricing`, `search`, `gateway` (in some order).

2. Start the chain and look at the order:

   ```bash
   make up && docker compose ps -a
   ```

   Expect the database and the four services `Up ... (healthy)` and the init job `Exited (0)`.

3. Reach a service by name from inside another container:

   ```bash
   docker compose exec gateway python -c "import urllib.request; print(urllib.request.urlopen('http://search:8080/health/live').read())"
   ```

   Expect `b'{"status":"up"}'`.

4. Restart one service with a different setting, as the shedding experiment does:

   ```bash
   SEARCH_CONCURRENCY_LIMIT=3 docker compose up -d --force-recreate --no-deps search
   ```

   Expect `Container storm-search-1  Started`. (Compose reads `SEARCH_CONCURRENCY_LIMIT` from the environment for the `${...}` in the file.)

5. Tear down:

   ```bash
   make down
   ```

## Mistakes people make

- **Assuming `depends_on` waits for readiness.** It does not, unless you give it a `condition` and the service has a health check.
- **Publishing ports on all interfaces** (`"8080:8080"`) when only local access is wanted.
- **Putting secrets in the compose file** instead of `.env` (which is ignored by git).
- **Forgetting `-v` on `down`** and being surprised that old database data is still there on the next start.
- **Relying on start order for correctness.** A service should still retry its connections; this repository's pool does.

## Check yourself

1. How does one container reach another?
   Answer: by the service name on the project's private network.
2. What does `${VAR:?message}` do?
   Answer: stops with the message if the variable is unset or empty.
3. What does `condition: service_completed_successfully` wait for?
   Answer: a one-shot container to exit with code 0.
4. What does `down -v` remove that `down` alone does not?
   Answer: the volumes (here, the databases' data).
5. Why do some top-level keys start with `x-`?
   Answer: Compose ignores them, so they can hold reusable blocks.

## Further reading

- Docker Compose documentation (Compose v2, the `docker compose` command): https://docs.docker.com/compose/
- The Compose file reference: https://docs.docker.com/reference/compose-file/
- Interpolation: https://docs.docker.com/reference/compose-file/interpolation/
- Startup order: https://docs.docker.com/compose/how-tos/startup-order/
