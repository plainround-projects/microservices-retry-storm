# 2. The stock store has finite capacity and finishes work its caller abandoned, unless the caller sent a deadline

Status: accepted, 2026-10-27

## Context

A retry storm outlives the slowdown that starts it because requests already issued keep arriving after the cause is gone. That only happens when the bottom of the chain keeps working on requests whose callers have timed out. A stock store that answers instantly, or that cancels work when the client disconnects, would show recovery in seconds even with naive retries and the experiment would prove nothing.

## Decision

The stock store works on at most `STOCK_CONCURRENCY` requests at a time (a semaphore, default 16), sleeps for the injected latency inside that limit, and then reads PostgreSQL. A request without a deadline header is carried to the end even if the caller left long ago (the web framework does not cancel a handler when the client disconnects). A request with an `X-Deadline-Ms` header runs inside `asyncio.timeout(remaining)`, so it stops waiting for capacity and stops "working" the moment its deadline passes.

## Consequences

- With the naive profile and 1.5 seconds of latency, capacity is about 11 requests per second against a load several times higher, a queue builds, and it drains only after the latency is removed.
- With deadlines, abandoned work costs nothing, which is the mechanism behind the fast recovery.
- The numbers depend on `STOCK_CONCURRENCY` and the load; they are parameters, not constants of nature.
