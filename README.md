# microservices-retry-storm

A chain of four small services, gateway to search to pricing to a stock store, that turns a small slowdown at the bottom into an outage at the top when every client retries three times, and then stops it layer by layer with a deadline that travels down, one retry layer with a budget, a circuit breaker and load shedding, measured on counters and traces.

| | |
| --- | --- |
| **Lesson** | `/prep/microservices/project-retry-storm`, "Cause a retry storm, then stop it" (`data-structure-explainer/src/content/tracks/microservices/34-project-retry-storm.md` in the Plainround app) |
| **Planned hours** | 16 |
| **Level** | Advanced |
| **Architecture** | [ARCHITECTURE.md](ARCHITECTURE.md), the policy page of step 1: [docs/design/policy.md](docs/design/policy.md), decisions: [docs/adr](docs/adr), raw results: [docs/results](docs/results) |

## Setup, run, test, stop

```bash
make up                  # the stock database (schema, seed), and the four services; the first run builds the images
make run                 # one traced request through the chain, then the step 2 measurement
make smoke               # one request with its trace printed as a tree
make design              # step 1: the policy page checked against the numbers in the code
make experiment-naive    # step 2: calls per layer with naive retries and 30 percent failures at the stock store
make experiment-storm    # step 3: the stock store slows to 1.5 s under load, then recovers
make experiment-policy   # step 4: deadlines, one retry layer with a budget (both measurements again)
make experiment-outage   # step 5: the stock store stopped entirely, under the full policy
make experiment-shedding # step 5: search sheds the excess with 503 and Retry-After
make experiment-full     # step 6: the step 3 experiment again with the full policy
make demo                # every experiment in lesson order (about 25 minutes)
make queries             # the stock store's sql/003_queries.sql
make check               # format check, lint, types, import rules, unit tests with coverage, the in-process chain tests
make test-integration    # the stock adapter against its database (needs make up first)
make test-system         # the chain across the running containers (needs make up first)
make audit               # known-vulnerability check of the locked dependencies
make down                # stops everything and deletes the database
```

`make help` lists every target. Each experiment restarts the four services first, so counters, caches, the breaker and the queues start empty, and writes its raw result to `docs/results/`. `LOAD_RPS=20 make experiment-storm` changes the request rate (default 5 per second). Operational log lines (one JSON object per line) go to standard error and carry the trace id as `correlation_id`.

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Docker with Compose v2 | 24 or newer (tested with 24.0.6), about 2 GB of free memory | the four services and the stock database |
| uv | 0.11 or newer (tested with 0.11.2) | the Python environment and the lock file |
| Python | 3.12 (uv installs it if missing; the images use 3.12.8) | the services, the experiments and the tests |
| GNU Make | 3.81 or newer | every command below |
| PostgreSQL image | `postgres:18` | the stock store's database, pulled on first `make up` |
| Python packages | FastAPI, uvicorn, httpx, psycopg 3 and prometheus-client at run time; pytest, pytest-asyncio, ruff, mypy, import-linter and pip-audit for development; all pinned by `uv.lock` | everything |

No cloud account. The database password is read from the environment, never from code: `make up` copies `.env.example` to `.env` the first time (a throwaway value for a database that lives only on this machine while the container runs). Everything is published on 127.0.0.1 only. Ports 43080 to 43083 (gateway, search, pricing, stock) and 43432 (the stock database) must be free; set them in `.env` to change them.


## Done when: the lesson's checklist and the proof for each line

Numbers are from runs on 2026-10-27 on a shared laptop that was busy with other work (load average 40 to 60 on 12 cores), at 5 requests per second; the machine, the load and the stock capacity (16 concurrent requests) are parameters, so the shape matters, not the digits. Raw results: [docs/results](docs/results).

| # | Done when (from the lesson) | Command | What proves it |
| --- | --- | --- | --- |
| 1 | The per-hop timeouts add up to less than the end-to-end deadline, and only one layer is allowed to retry. | `make design` | [docs/design/policy.md](docs/design/policy.md) has the table (1800, 1200, 700 ms inside 2000 ms, 50 ms margins; retries at the gateway only). `tests/system/test_policy_design.py` reads it and checks the nesting, that exactly one row retries, and that every number equals the constant in the code. |
| 2 | Your counters show calls multiplying hop by hop, and the README has the ratio of stock-store calls to gateway requests. | `make experiment-naive` | Over 45 s with 30 percent failures at the stock store and the naive profile: gateway 226, search 226, pricing 230, stock 304. **Ratio stock:gateway = 1.35x.** It is modest because a 30 percent error rate costs 1 + 0.3 + 0.09 = 1.39 attempts per request at the bottom hop and almost never reaches the hops above; the multiplication the lesson draws (3, 9, 27) appears when failures are timeouts, which is step 3. |
| 3 | A trace shows one user request fanning out into many stock-store calls, and the stock store's load stays high after you remove the latency, which you explain. | `make experiment-storm` | With the stock store at 1.5 s the naive chain goes from 0% errors to 99% errors with p99 6108 ms, and the stock store's in-flight requests peak at 3405 (baseline 1). The trace of one slow request is below. Every request issued during the slowdown, by the gateway, by search and by pricing, is still queued at the store and is carried to the end (ADR 2), so the load outlives its cause. In this run the chain then did not recover within the 240 s cap (table below): once calls start timing out, the retries alone turn 5 gateway requests per second into about 45 calls per second at pricing and 135 at the store, more than it can answer inside a 2 second timeout, so the failure sustains itself until the load stops. (A run on a less busy machine recovered in about 30 s; both outcomes follow from the same arithmetic, and neither is bounded.) |
| 4 | The stock-store-to-gateway call ratio drops close to 1, and recovery after removing the latency takes seconds, not minutes. | `make experiment-policy` | With deadlines, one retry layer and the budget: gateway 226, search 227, pricing 227, stock 227, **ratio 1.0x**, with 30% of answers degraded (the injected failures, answered with a stale price instead of a retry) and no errors. Under the 1.5 s slowdown the stock store's in-flight peak is 9, and recovery takes 10 s. |
| 5 | With the stock store fully down, the gateway still answers within its deadline with a clear degraded response, and the stock store sees almost no calls until it recovers. | `make experiment-outage` (and `make experiment-shedding` for the 503 with `Retry-After`) | Stock stopped for 30 s under the full policy: 121 requests, 0% errors, 100% degraded answers, p99 125 ms (deadline 2000 ms). Pricing sent 30 attempts to the dead store for 155 requests (the breaker took a few failures to open) and refused 124 locally. After the restart, answers are fresh again (0% degraded). Shedding: with search limited to 3 requests in progress and pricing slow, search refused 217 requests with 503 and `Retry-After: 1`. |
| 6 | The README shows p99, error rate, amplification ratio and recovery time before and after, with one trace for each. | `make experiment-full` and the table below | The table and the two traces below. |

### Before and after

| profile | gateway p99 before the slowdown | p99 under the slowdown | answers under the slowdown | stock in-flight peak | recovery after the latency is removed |
| --- | --- | --- | --- | --- | --- |
| naive (step 3) | 330 ms | 6108 ms | 99% errors, 0% degraded | 3405 | did not recover within the 240 s cap |
| deadline and one retry layer with a budget (step 4) | 183 ms | 1140 ms | 0% errors, 100% degraded | 9 | 10 s |
| full policy: plus breaker and shedding (step 6) | 397 ms | 908 ms | 0% errors, 100% degraded | 3 | 13 s |

Amplification (stock-store calls per gateway request, same load, 30 percent failures at 50 ms): naive **1.35x**, with the policy **1.0x**.

"Recovery" is the time from removing the latency until, for two consecutive checks, the last 10 seconds of gateway answers have no more errors or degraded answers than the baseline plus a small margin, a p99 within twice the baseline's, and the stock store's in-flight count back under three times its baseline peak. The 30 percent failures stay on throughout, so the baseline itself contains degraded answers under the policy.

One request during the slowdown, naive profile (each indent is a call; `attempt` is the retry number; `[timeout]` is the caller giving up, while the store keeps working):

```text
gateway /search        +     0.0 ms    6003.4 ms  [503]
  call search            +     0.2 ms    2000.8 ms  [timeout] attempt=1
    search /search         +     1.6 ms    6003.0 ms  [502]
      call pricing           +     1.8 ms    2000.7 ms  [timeout] attempt=1
      call pricing           +  2002.5 ms    2000.5 ms  [timeout] attempt=2
      call pricing           +  4003.1 ms    2001.4 ms  [timeout] attempt=3
  call search            +  2001.0 ms    2000.5 ms  [timeout] attempt=2
    search /search         +  2003.5 ms    6006.5 ms  [502]
      call pricing           +  2003.6 ms    2002.0 ms  [timeout] attempt=1
      call pricing           +  4005.6 ms    2003.1 ms  [timeout] attempt=2
      call pricing           +  6008.9 ms    2000.9 ms  [timeout] attempt=3
  call search            +  4001.6 ms    2001.6 ms  [timeout] attempt=3
    search /search         +  4004.0 ms    6005.2 ms  [502]
      call pricing           +  4004.2 ms    2001.5 ms  [timeout] attempt=1
      call pricing           +  6005.7 ms    2002.7 ms  [timeout] attempt=2
      call pricing           +  8008.6 ms    2000.6 ms  [timeout] attempt=3
stock /stock           +     5.0 ms   24886.7 ms  [200]
stock /stock           +  2009.1 ms   25054.3 ms  [200]
stock /stock           +  2009.4 ms   25054.0 ms  [200]
stock /stock           +  2009.8 ms   25053.7 ms  [200]
stock /stock           +  4015.4 ms   24269.8 ms  [200]
stock /stock           +  4016.0 ms   24258.4 ms  [500]
stock /stock           +  4016.3 ms   24269.0 ms  [200]
stock /stock           +  4016.5 ms   24312.5 ms  [200]
stock /stock           +  4016.8 ms   24310.6 ms  [500]
stock /stock           +  4016.9 ms   24311.7 ms  [200]
stock /stock           +  6027.5 ms   23533.3 ms  [200]
stock /stock           +  6027.8 ms   23535.5 ms  [200]
stock /stock           +  6027.9 ms   23538.6 ms  [200]
stock /stock           +  6028.1 ms   23538.0 ms  [200]
stock /stock           +  6028.2 ms   23551.6 ms  [200]
stock /stock           +  6029.4 ms   23551.9 ms  [200]
stock /stock           +  6029.5 ms   23562.2 ms  [200]
stock /stock           +  8037.5 ms   22832.9 ms  [200]
stock /stock           +  8042.4 ms   22894.1 ms  [200]
stock /stock           +  8043.3 ms   22893.3 ms  [200]
stock /stock           +  8043.5 ms   22893.2 ms  [200]
stock /stock           +  8043.7 ms   22876.0 ms  [500]
stock /stock           +  8044.0 ms   22877.2 ms  [500]
stock /stock           + 10019.4 ms   22046.8 ms  [200]
stock /stock           + 10031.2 ms   22037.8 ms  [200]
stock /stock           + 10031.5 ms   22048.6 ms  [200]
stock /stock           + 12020.6 ms   22537.1 ms  [200]
```

The same kind of request during the slowdown, full policy:

```text
gateway /search        +     0.0 ms       9.6 ms  [200]
  call search            +     0.6 ms       8.6 ms  [200] attempt=1
    search /search         +     2.0 ms       6.5 ms  [200]
      call pricing           +     2.6 ms       5.6 ms  [200] attempt=1
        pricing /price         +     4.9 ms       0.8 ms  [200]
```

A healthy request for comparison, `make smoke`:

```text
gateway /search        +     0.0 ms     141.9 ms  [200]
  call search            +     0.9 ms     140.5 ms  [200] attempt=1
    search /search         +     5.3 ms     134.6 ms  [200]
      call pricing           +     5.7 ms     133.6 ms  [200] attempt=1
stock /stock           +    15.3 ms      56.3 ms  [500]
stock /stock           +    77.2 ms      55.8 ms  [200]
```

The same experiments run as the lesson's tests in two places: the mechanisms (retries multiply 3, 9, 27 and 135 calls for 5 requests; the budget caps retries; the breaker opens after 10 failures; search sheds) run in one process in `tests/system/test_chain_in_process.py` on every `make check`, and the chain across containers runs in `tests/e2e` (`make test-system`).

## Stretch goals

| Stretch (from the lesson) | Built? |
| --- | --- |
| Make two different services share the stock store and show a bulkhead keeping one healthy while the other is overloaded. | No. ARCHITECTURE.md, "How to extend it", describes the change: a separate `ConcurrencyLimiter` per caller in the stock store. |
| Replace the fixed concurrency limit with an adaptive one and compare under a sudden doubling of load. | No. The limiter is a plain counter behind a small class, so an adaptive policy replaces one object. |
| Add hedged requests at the gateway for reads only, and show the effect on p99 and on total load. | No. |

## What is simplified for teaching

Only what the lesson scopes out, plus where this build differs from the lesson's text:

- The lesson's services are Flask and `requests`; these are FastAPI and `httpx` on asyncio, because the experiment needs hundreds of requests in flight on one process.
- The lesson counts "retries three times" as 3 attempts in its code (`range(3)`), 4 attempts in its arithmetic (4 x 4 x 4 = 64), and 3 attempts in its diagram (3, 9, 27). The code and the diagram agree, so the naive profile makes 3 attempts per hop; the in-process test shows 5, 15, 45 and 135 calls for 5 requests when the stock store always fails.
- The lesson's numbers (3.25x, 480 in flight, minutes of recovery) depend on its machine and load. These measurements use a request rate low enough for a busy laptop (5 per second by default, `LOAD_RPS`) and a stock store that works on 16 requests at a time (`STOCK_CONCURRENCY`); with those parameters the naive chain collapses the same way and the policy contains it.
- Tracing is a hand-rolled W3C trace context with spans kept in memory for the requests sampled or forced, not an OpenTelemetry pipeline; the trees above are printed from those spans. The lesson's "trace screenshot" is this text tree.
- Search and pricing keep no database: search resolves a query by a fixed rule and pricing derives a base price from the sku, caching its last answer in memory. The stock store has the only database.
- The admin routes that switch the profile and the faults have no authentication; every port is published on 127.0.0.1 only.
- One instance of each service: the breaker, the shedding limit and the retry budget are per process.

## Verification notes

- `make check` (format check, lint, strict types, import rules, unit tests with at least 95 percent coverage of the domain and application layers and of `resilience`, and the in-process chain tests), `make audit`, `make test-integration` and `make test-system` pass; the last two ran against the Docker stack.
- Docker on the build machine was unstable during the session (its disk filled and the virtual disk went read-only; later it stopped responding for stretches). The amplification and slowdown measurements of the naive profile are from the four services run as host processes against a local PostgreSQL, because the container run of those two was taken while the machine was starved and was discarded; every other measurement is from the Docker stack. The same code ran in both.

## Project structure

```text
services/
  gateway/                the edge: the deadline, the only retry loop (backoff, jitter, retry budget), 503 with Retry-After
    src/gateway/
      domain/             the answer type. Depends on the standard library only.
      application/        HandleSearch and its ports (SearchUpstream, Telemetry). Depends on domain and resilience.
      infrastructure/     the HTTP call to search, Prometheus telemetry, settings. Depends on application and domain.
      interfaces/         the HTTP API, the composition root and the entry point. Nothing depends on it.
    tests/                unit/
  search/                 query resolution, the concurrency limit and load shedding, the partial result
  pricing/                the price rule, the circuit breaker, the last-known-price fallback
  stock/                  the capacity-limited store with injected latency and failures; sql/ and integration tests
libs/
  resilience/             Deadline, Backoff, RetryBudget, CircuitBreaker, ConcurrencyLimiter, the retry loops, the
                          profiles. Plain objects, no I/O, every clock injected.
  chassis/                settings, JSON logging, W3C trace context and spans, the deadline context, metrics, one-attempt
                          upstream calls, health, the async PostgreSQL pool. Infrastructure only.
docs/                     design/policy.md (the one-page policy of step 1), adr/ (decisions), results/ (raw
                          measurements and the trace trees of the runs quoted below)
scripts/                  apply-sql.sh, experiment.py (the measurements), trace.py (one traced request)
tests/
  system/                 the load generator, harness, experiments, and the check of the policy page against the code
  e2e/                    the chain across the running containers
docker-compose.yml        the stock database and its init job, the four services
Dockerfile                one image per service (--build-arg SERVICE=...), non-root, pinned base image
pyproject.toml, uv.lock   the uv workspace and the lock file
Makefile                  the only commands a reader needs (make help)
```

The dependency rule (domain depends on nothing; application on domain and resilience; infrastructure and interfaces on both; nothing on interfaces) is enforced by `make lint` through import-linter, per service. A second contract forbids `domain` and `application` from importing FastAPI, psycopg, httpx, prometheus-client or the chassis.

## The SQL

The stock store is the only service with data. Its `services/stock/sql/` holds `001_schema.sql` (the `stock_levels` table, the read-only application role), `002_seed.sql` (the 20 skus, four of them under ten units) and `003_queries.sql` (the queries behind the price surcharge); `make db` applies the first two and `make queries` runs the third. Search and pricing keep no data of their own: search resolves a query to a sku by a fixed rule, and pricing derives a base price from the sku and caches its last answer in memory, which is the fallback the lesson's diagram calls "cached answer".

## Author and license

Written by Sumit Srivastava. Released under the MIT License, see [LICENSE](LICENSE). Changes are in [CHANGELOG.md](CHANGELOG.md).
