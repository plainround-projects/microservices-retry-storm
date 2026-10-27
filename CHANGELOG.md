# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `tech/`: one note per technology the repository uses, from zero, with an index.

### Fixed

- `make help` printed descriptions without target names.

## [0.1.0] - 2026-10-27

### Added

- A chain of four services (gateway, search, pricing, stock) with the stock store backed by its own PostgreSQL 18 database, in `docker-compose.yml`.
- Three resilience profiles switchable at runtime: naive (three immediate retries at every hop), deadline plus a single retry layer with backoff, jitter and a 10 percent retry budget, and the full policy with a circuit breaker on pricing's call to stock and load shedding at search.
- `libs/resilience`: deadline, backoff with full jitter, retry budget, circuit breaker, concurrency limiter and the two retry loops, as plain objects with injected clocks.
- A capacity-limited stock store with injectable latency and failure rate, controllable at runtime.
- W3C trace context across every hop, spans kept in memory for sampled traces and rendered as a tree, JSON logs with the trace id as correlation id, Prometheus counters at every service.
- An open-loop load generator and the lesson's experiments (amplification, collapse and recovery, deadlines and budget, outage with the breaker, shedding, the full policy) with results written to `docs/results/`.
- Unit, integration and system tests, ruff, mypy (strict), import-linter, pip-audit and a CI workflow with a matrix over the packages.
