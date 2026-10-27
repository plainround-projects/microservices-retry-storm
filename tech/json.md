# JSON

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

JSON (JavaScript Object Notation) is a text format for data. Almost every programming language can read and write it, so it is the usual way for programs to send data to each other over HTTP and to keep small data files.

## Why this repository uses it

The services talk to each other in JSON over HTTP, the control routes take JSON (switch a profile, inject latency), the experiments write their raw results as JSON, and the logs are JSON lines. The committed result files in `docs/results/` are the evidence behind the lesson's claims, and they are readable because JSON is.

## Where it appears

| Path | What it does with JSON |
| --- | --- |
| `docs/results/*.json` | Raw results of each experiment: calls received per service, ratios, client latency percentiles |
| `services/*/src/*/interfaces/api.py` | Request and response bodies |
| `libs/chassis/src/chassis/admin.py` | `POST /admin/profile` takes `{"name": "full"}`; `GET /admin/traces/{id}` returns spans as JSON |
| `libs/chassis/src/chassis/logging.py` | Each log line is one JSON object |
| `tests/system/harness.py`, `experiments.py` | Send JSON, read JSON, write the result files |

## How it works underneath

- An **object** is `{ "name": value, ... }`: keys are always text in double quotes. An **array** is `[ value, value ]`. A value is text in double quotes, a number, `true`, `false`, `null`, an object or an array.
- JSON has no comments, no trailing commas, and no single quotes. Numbers have no exact decimal type, which is why money is often sent as text.
- A program reads JSON text into its own values (in Python: dictionaries, lists, strings, numbers, booleans, `None`) with `json.loads`, and writes values back with `json.dumps`. An HTTP header `Content-Type: application/json` tells the receiver how to read a body.
- A web API and its callers must agree on the **field names** and types. That agreement is called a **contract**. In a **consumer-driven** contract the caller writes down what it sends and what it needs back; the provider's tests then check it still honors that.

## Walkthrough of this repository's use

### A result file

```json
{
  "profile": "deadline-retry",
  "fail_rate": 0.3,
  "latency_ms": 50,
  "rps": 5.0,
  "received": {
    "gateway": 226.0,
    "search": 227.0,
    "pricing": 227.0,
    "stock": 227.0
  },
  "ratios": {
    "search:gateway": 1.0,
    "pricing:gateway": 1.0,
    "stock:gateway": 1.0
  },
  "gateway_retries": 1.0,
  "client": {
    "requests": 225,
    "p50_ms": 111,
    "p99_ms": 1817,
    "error_rate": 0.0044,
    "degraded_rate": 0.3022,
    "statuses": {
      "503": 1,
      "200": 224
    }
  }
}
```

This is the measured outcome of lesson step 4 (`deadline-retry` profile; 30 percent failures injected at the stock store; 5 requests per second). Read it from the top:

- `received` is how many business requests each service received during the run. `ratios` divides each by the gateway's count: with one attempt per hop the amplification is 1.0. The naive profile's file, `amplification-naive.json`, has ratios above 1.
- `gateway_retries` is how many retries the gateway issued.
- `client` is what the load generator saw: `p50_ms` and `p99_ms` are the median and the 99th-percentile latency in milliseconds, `error_rate` the share of failed requests, `degraded_rate` the share answered with a partial result, and `statuses` a count per HTTP status.
- Numbers are plain JSON numbers; `statuses` has numbers as text keys, because JSON object keys are always text.

### A JSON body in a request

```python
class ProfileBody(BaseModel):
    """``POST /admin/profile``."""

    name: str

```

`ProfileBody` is the shape of `{"name": "full"}` (see `pydantic.md`). The experiments send it with `httpx` and `json={"name": name}` (see `httpx.md`):

```python
async def set_profile(name: str) -> None:
    """Switch every service (except stock, which has no profile) to a resilience profile."""
    async with httpx.AsyncClient(timeout=30) as client:
        for service in ("gateway", "search", "pricing"):
            answer = await client.post(f"{url(service)}/admin/profile", json={"name": name})
            answer.raise_for_status()
```

### JSON as the log format

```python
        payload: dict[str, Any] = {
            "ts": stamp,
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        correlation_id = current_correlation_id()
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = _mask(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))
```

`json.dumps(payload, default=str, separators=(",", ":"))` writes one compact object per line. `default=str` turns anything JSON cannot hold, such as a time, into text. One object per line is easy for tools to read and for `grep` to search; the trace id is the correlation id on each line.

### Spans as JSON

```python
    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for the trace endpoint."""
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentId": self.parent_id,
            "service": self.service,
            "name": self.name,
            "startMs": round(self.start_ms, 3),
            "durationMs": round(self.duration_ms, 3),
            "status": self.status,
            "attributes": self.attributes,
        }
```

`as_dict` turns a finished span into a dictionary with JSON-friendly names (`traceId`, `startMs`, `durationMs`). `GET /admin/traces/{trace_id}` returns a list of these; `scripts/trace.py` fetches them and prints the tree (see `python.md`).

### Every file in `docs/results/`

Each experiment writes its raw result here (`_save` in `tests/system/experiments.py`), and the files are committed on purpose as evidence.

| File | Experiment | Keys |
| --- | --- | --- |
| `amplification-naive.json`, `amplification-deadline-retry.json` | steps 2 and 4 | `profile`, `fail_rate`, `latency_ms`, `rps`, `received`, `ratios`, `gateway_retries`, `client` (read above) |
| `slowdown-naive.json`, `slowdown-deadline-retry.json`, `slowdown-full.json` | steps 3 and 6 | `profile`, `fail_rate`, `slow_ms`, `rps`, `baseline`, `slowdown`, `stock_in_flight`, `after_reset_windows`, `recovery_seconds`, `recovery_capped_at_s`, `recovery_threshold_in_flight`, `recovery_p99_limit_ms`, `traces`, `stock_spans_in_slowdown_trace` |
| `outage-full.json` | step 5, stock stopped | `rps`, `before_outage`, `during_outage`, `after_restart`, `requests_during_outage`, `pricing_attempts_sent_to_stock_during_outage`, `pricing_calls_refused_by_the_breaker`, `breaker_state_at_end_of_outage`, `forced_request` |
| `shedding-full.json` | step 5, load shedding | `rps`, `limit`, `client`, `search_shed`, `gateway_retries`, `gateway_outcomes` |
| `*-trace-*.txt` | a rendered span tree | text, not JSON |

In the slowdown files, `baseline`, `slowdown` and each entry of `after_reset_windows` are windows of the same shape as `client` above (`requests`, `p50_ms`, `p99_ms`, `error_rate`, `degraded_rate`, `statuses`); `stock_in_flight` holds the stock store's concurrent-request gauge (`baseline_peak`, `slowdown_peak`, and `after_reset_every_3s` as a list of `[seconds, value]` pairs); `recovery_seconds` is `null` (JSON's "no value") when the system did not recover within the cap.

## Try it yourself

1. Read two result files and compare their amplification:

   ```bash
   uv run python -c "
   import json
   for name in ('amplification-naive', 'amplification-deadline-retry'):
       d = json.load(open(f'docs/results/{name}.json'))
       print(name, d['ratios'], d['client']['p99_ms'])"
   ```

   Expect two lines. The naive one shows ratios above 1 (`stock:gateway` is 1.35) and the other shows `1.0` for all three.

2. Check that a file is valid JSON, with a tool that reports the position of an error:

   ```bash
   printf '{"a": 1,}' | uv run python -m json.tool
   ```

   Expect `Expecting property name enclosed in double quotes: line 1 column 9 (char 8)`.

3. Print a log line by hand:

   ```bash
   uv run python -c "import logging; from chassis.logging import configure_logging; configure_logging('demo'); logging.getLogger('x').info('hello', extra={'path': '/stock', 'api_key': 'secret'})"
   ```

   Expect one JSON line with `"msg":"hello"`, `"path":"/stock"` and `"api_key":"***"` (the key name looks secret, so the value is masked).

## Mistakes people make

- **Trailing commas and comments,** which make a file invalid.
- **Single quotes** instead of double quotes.
- **Sending money or large ids as numbers.** Precision may be lost; use text.
- **Renaming a field** without telling the callers: a breaking change.
- **Reading a missing key as if it were present.** Check it, or let a validator (see `pydantic.md`) do it.

## Check yourself

1. Which quotes does JSON allow?
   Answer: double quotes only.
2. Can JSON hold a comment?
   Answer: no.
3. What is a contract between two services?
   Answer: the agreed field names, types and status codes of their messages.
4. What does "consumer-driven" mean?
   Answer: the caller writes down what it relies on, and the provider is tested against it.
5. How does Python turn JSON text into values?
   Answer: `json.loads`.

## Further reading

- The JSON specification, RFC 8259: https://www.rfc-editor.org/rfc/rfc8259
- json.org (the grammar in diagrams): https://www.json.org/
- Python's `json` module: https://docs.python.org/3.12/library/json.html
