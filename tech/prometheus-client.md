# prometheus_client and the Prometheus text format

## What it is

`prometheus_client` is a Python library for keeping **metrics**: numbers that describe how a program is behaving, such as how many requests it has served. Prometheus is a monitoring system that reads metrics from programs; its format is the common way to expose them. Here no Prometheus server is run; the experiments read the metrics directly.

A program exposes its metrics at a URL, usually `/metrics`, as plain text. Each metric has a name, optional **labels** (key and value pairs that split one metric into several series), and a value.

## Why this repository uses it

The lesson's proof of amplification is a number: how many requests each service received while the gateway served N. Every service counts what it receives and what it sends, and exposes the counts at `/metrics`. The experiments read the counters before and after a run and divide, so the claim "the stock store saw 1.35 times what the gateway saw" comes from the services themselves, not from the load generator.

## Where it appears

| Path | What it does |
| --- | --- |
| `libs/chassis/src/chassis/metrics.py` | `ServiceMetrics` (the registry and the four common metrics) and the `/metrics` route |
| `services/*/src/*/infrastructure/telemetry.py` | The service-specific counters (`retries_total`, `outcomes_total`, breaker and shedding counts) |
| `libs/chassis/src/chassis/observability.py` | Updates the common metrics for each request |
| `libs/chassis/src/chassis/upstream.py` | Counts attempts sent |
| `tests/system/harness.py` | Reads and parses `/metrics` |
| `pyproject.toml`, `libs/chassis/pyproject.toml` | The dependency `prometheus-client>=0.20` |

## How it works underneath

- A **Counter** only goes up (requests served). A **Gauge** goes up and down (requests in progress). A **Histogram** counts observations into buckets (how many requests took under 10 ms, under 25 ms, and so on) so that percentiles can be estimated.
- A **registry** is the set of metrics a program exposes. `generate_latest(registry)` renders them as text in the exposition format: `# HELP` and `# TYPE` comment lines, then one line per series, `name{label="value"} number`.
- Counters in this library also produce a `_created` series, the time the counter was created. A counter name conventionally ends in `_total`.
- A reader takes two readings and subtracts: the difference of a counter over a time window is the number of events in that window. Dividing the difference at one service by the difference at another gives a ratio.
- Labels multiply the number of series. A label with an unbounded set of values (a user id) creates unbounded series and harms the monitoring system; labels must come from a small fixed set.

## Walkthrough of this repository's use

### The registry and the common metrics

```python
class ServiceMetrics:
    """A registry with the metrics every service has; services add their own with :meth:`counter` and :meth:`gauge`.

    ``calls_received_total`` counts every business request that arrives, including the ones refused at once, so the
    ratio of two services' counters over the same window is the amplification between them.
    """

    def __init__(self, service: str) -> None:
        self.service = service
        self.registry = CollectorRegistry()
        self.received = Counter("calls_received_total", "Requests received", ["route"], registry=self.registry)
        self.sent = Counter("calls_sent_total", "Attempts sent to another service", ["target"], registry=self.registry)
        self.in_flight = Gauge("in_flight", "Requests being handled", registry=self.registry)
        self.duration = Histogram(
            "request_duration_seconds",
            "Time to handle a request",
            ["route", "status"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0),
            registry=self.registry,
        )

    def counter(self, name: str, documentation: str, labels: list[str] | None = None) -> Counter:
        """Register a counter in this service's registry."""
        return Counter(name, documentation, labels or [], registry=self.registry)

    def gauge(self, name: str, documentation: str, labels: list[str] | None = None) -> Gauge:
        """Register a gauge in this service's registry."""
        return Gauge(name, documentation, labels or [], registry=self.registry)
```

- `CollectorRegistry()` is a private registry for this service (not the library's global one), so four apps in one test process do not share counters.
- `calls_received_total{route}` counts every business request that arrives, including the ones refused at once: "so the ratio of two services' counters over the same window is the amplification between them", as the docstring says.
- `calls_sent_total{target}` counts attempts sent to another service, labeled by the service name.
- `in_flight` is a gauge: incremented at the start of a request, decremented in a `finally`.
- `request_duration_seconds{route,status}` is a histogram with the bucket edges listed (10 ms up to 10 s), chosen around the 2-second deadline.
- `counter(...)` and `gauge(...)` let each service add its own.

### Exposing them

```python
def metrics_router(metrics: ServiceMetrics) -> APIRouter:
    """``GET /metrics`` in the Prometheus text format."""
    router = APIRouter(tags=["metrics"])

    @router.get("/metrics")
    async def scrape() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return router
```

`generate_latest(registry)` renders the text; `CONTENT_TYPE_LATEST` is the content type Prometheus expects.

### Service-specific counters

```python
class PrometheusTelemetry:
    """Implements the ``Telemetry`` port."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._retries = metrics.counter("retries_total", "Retries issued by the gateway", ["reason"])
        self._outcomes = metrics.counter("outcomes_total", "How requests ended", ["outcome"])

    def retry(self, reason: str) -> None:
        """A retry was issued."""
        label = reason if reason in {"timeout", "status 500", "status 502", "status 503", "status 504"} else "other"
        self._retries.labels(label).inc()

    def outcome(self, outcome: str) -> None:
        """The request ended as ``ok``, ``degraded`` or ``unavailable``."""
        self._outcomes.labels(outcome).inc()
```

The gateway adds `retries_total{reason}` and `outcomes_total{outcome}`. `retry` maps unknown reasons to the label `other`, which keeps the set of label values small and fixed (see the mistakes above). The application layer talks to a `Telemetry` port, so it never imports the library; `PrometheusTelemetry` is the adapter.

### Reading them back

The experiments fetch `/metrics` from each service and look up values with `harness.metric(values, "calls_received_total", route="/stock")` (tested in `tests/system/test_harness.py`). They take one reading at the start of a window and one at the end and divide the differences: `ratios` in the result files.

## Try it yourself

1. Create a registry and look at the exposition text:

   ```bash
   uv run python -c "
   from prometheus_client import generate_latest
   from chassis.metrics import ServiceMetrics
   m = ServiceMetrics('demo')
   m.received.labels('/stock').inc(); m.received.labels('/stock').inc()
   print(generate_latest(m.registry).decode().splitlines()[:3])"
   ```

   Expect `['# HELP calls_received_total Requests received', '# TYPE calls_received_total counter', 'calls_received_total{route="/stock"} 2.0']`.

2. With the stack up, read a real service's counters:

   ```bash
   curl -s localhost:43083/metrics | grep -E "^(calls_received_total|in_flight)"
   ```

   Expect `in_flight 0.0` when idle and a `calls_received_total{route="/stock"}` line once requests have arrived.

3. Send a few requests and watch the counter move:

   ```bash
   for i in 1 2 3; do curl -s -o /dev/null -X POST localhost:43080/search -H 'content-type: application/json' -d '{"query":"widget"}'; done; curl -s localhost:43080/metrics | grep "^calls_received_total"
   ```

   Expect `calls_received_total{route="/search"}` to have grown by 3.

## Mistakes people make

- **Unbounded label values,** such as ids or free text.
- **Using a gauge for something that only goes up,** or a counter for something that goes down.
- **One global registry shared by services in the same process.** In tests with several apps in one process, the metrics mix; give each service its own registry, as this repository does.
- **Reading a counter's absolute value as a rate.** Take the difference over a window.
- **Forgetting to expose the registry** at a route.

## Check yourself

1. What is the difference between a counter and a gauge?
   Answer: a counter only goes up; a gauge goes up and down.
2. What does a histogram record?
   Answer: how many observations fell into each bucket, so percentiles can be estimated.
3. What is a label?
   Answer: a key and value that split a metric into separate series.
4. How do you get a rate from a counter?
   Answer: read it twice and divide the difference by the elapsed time.
5. Why one registry per service?
   Answer: so services running in the same process (as in the in-memory tests) do not mix their numbers.

## Further reading

- prometheus_client for Python (this repository pins 0.26): https://prometheus.github.io/client_python/
- Metric types: https://prometheus.io/docs/concepts/metric_types/
- Exposition formats: https://prometheus.io/docs/instrumenting/exposition_formats/
