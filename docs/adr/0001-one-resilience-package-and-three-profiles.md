# 1. The policy is a package of plain objects, and the chain switches between three profiles at runtime

Status: accepted, 2026-10-27

## Context

The lesson compares one chain under three policies: naive retries everywhere; deadlines, one retry layer and a retry budget; and that plus a circuit breaker and load shedding. Measuring all three needs the same code, the same data and the same machine; rebuilding between runs would add noise and take minutes.

## Decision

The policy lives in `libs/resilience` as objects with no I/O: `Deadline`, `Backoff`, `RetryBudget`, `CircuitBreaker`, `ConcurrencyLimiter` and two retry loops (`run_naive`, `run_budgeted`). Every clock, sleep and random source is a constructor argument, so each behavior is a unit test with a fake clock. Each service has a `ProfileHolder` and its use case reads the profile on every request; `POST /admin/profile` switches it, so an experiment changes the whole chain with four HTTP calls and no restart.

## Alternatives considered

- **Environment variables per run:** each profile change restarts four containers.
- **Three branches or three compose files:** the measurements would compare different code.

## Consequences

- The domain and application layers import `resilience` (pure) but not `chassis` (infrastructure); import-linter enforces it.
- The naive profile stays in the code of the final build because it is the baseline every comparison needs.
- The admin routes change behavior, so compose publishes every port on 127.0.0.1 only and the README says a real deployment would not mount them.
