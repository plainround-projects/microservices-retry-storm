# 3. W3C trace context with spans kept in memory, and Prometheus counters, instead of an OpenTelemetry pipeline

Status: accepted, 2026-10-27

## Context

The lesson asks for tracing across every hop and a counter per service so that amplification can be read as a ratio. It allows OpenTelemetry or "a hand-rolled trace id passed in a header and logged at every call".

## Decision

Every request carries a W3C `traceparent` header. The middleware joins or starts the trace, makes the trace id the correlation id of the log lines, and records a server span; the upstream client records one client span per attempt, with the attempt number. Sampled traces (forced per request with `X-Trace-Force`, or by `TRACE_SAMPLE_RATE` at the gateway) are kept in a bounded in-memory store per service and read back through `/admin/traces/{id}`; the harness merges the four services' spans into an indented tree. Counters (`calls_received_total`, `calls_sent_total`, retries, shed, breaker) are Prometheus counters on `/metrics`, read twice over the same window to get a ratio.

## Consequences

- No collector, no extra containers, and the trace of one request is one command.
- Spans are not exported anywhere and are lost on restart; the format is compatible, so swapping in OpenTelemetry later changes `chassis.tracing` and nothing else.
- Counting every span of a collapsed system would cost memory, so only sampled traces are stored.
