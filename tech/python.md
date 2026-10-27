# Python 3.12

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

Python is a programming language. You write instructions in text files that end in `.py`. A program called the interpreter reads those files and carries out the instructions, one by one. Python 3.12 is one specific version of the language. This repository needs version 3.12 or newer.

A **type hint** is a note you write next to a name, such as `port: int`. Python does not enforce it when the program runs. A separate tool (see `mypy.md`) reads the notes and warns you when they do not fit together.

## Why this repository uses it

The four services, the resilience library, the shared chassis and the measuring scripts are Python. The lesson is about how retries multiply and how a policy stops them. The policy objects (`Deadline`, `Backoff`, `RetryBudget`, `CircuitBreaker`, `ConcurrencyLimiter`) are plain Python classes that take their clock as an argument, so a test can run a ten-second scenario in a millisecond.

## Where it appears

| Path | What it does with Python |
| --- | --- |
| `libs/resilience/src/resilience/` | The policy as plain objects, no input or output |
| `libs/chassis/src/chassis/` | Shared service parts: tracing, metrics, middleware, the HTTP hop client, the database pool |
| `services/gateway/`, `search/`, `pricing/`, `stock/` | The four-service chain, each in four layers: `domain`, `application`, `infrastructure`, `interfaces` |
| `tests/system/`, `scripts/` | The experiments and the in-process chain |
| `.python-version` | `3.12` |

## How it works underneath

You start a program with `python file.py` or, here, through a command that `uv` runs for you. The interpreter first turns the file into a list of simple steps (bytecode), then runs them. The steps run in order, one thread at a time per process.

A **module** is one `.py` file. A **package** is a folder of modules. When one file says `from chassis.errors import ConfigError`, Python finds the package `chassis`, opens `errors.py` inside it, and gives you the name `ConfigError`. Where Python looks is decided by the packages installed in the project's virtual environment (see `uv.md`).

A **class** is a recipe for making objects. An **object** holds data and has functions (methods) that work on that data. `self` is the object a method is working on.

An **exception** is how Python reports "this cannot continue". `raise` throws one. `try`/`except` catches it. If nobody catches it, the program stops with a trace of the calls that led there.

## Walkthrough of this repository's use

### Immutable value objects with behavior

```python
@dataclass(frozen=True, slots=True)
class Deadline:
    """The moment (on a monotonic clock, in seconds) by which the whole request must be answered."""

    expires_at: float

    @classmethod
    def after(cls, now: float, seconds: float) -> Deadline:
        """A deadline ``seconds`` from ``now``."""
        return cls(now + seconds)
```

`@dataclass(frozen=True, slots=True)` writes `__init__` from the field list, makes the object read-only and fixes its attribute layout. `Deadline` holds one number, the moment (on a monotonic clock, in seconds) when the request must be answered. `@classmethod` receives the class (`cls`), so `Deadline.after(now, 2.0)` is an alternate constructor that returns a `Deadline`.

### Defensive parsing with early returns

```python
    @classmethod
    def from_header(cls, value: str | None, now: float, ceiling_seconds: float = 60.0) -> Deadline | None:
        """Parse the remaining milliseconds a caller sent; ignore a missing, malformed or negative value.

        A huge value is clamped to ``ceiling_seconds`` so a caller cannot make this service wait forever.
        """
        if value is None:
            return None
        try:
            remaining_ms = float(value)
        except ValueError:
            return None
        if remaining_ms != remaining_ms or remaining_ms < 0:
            return None
        return cls.after(now, min(remaining_ms / 1000.0, ceiling_seconds))
```

The method reads a header sent by another service. It treats everything about it as untrusted: missing (`None`), not a number (`ValueError` caught), not-a-number or negative (`remaining_ms != remaining_ms` is true only for NaN, since NaN is the one number not equal to itself), and too large (`min(..., ceiling_seconds)`). The return type `Deadline | None` says "a deadline, or nothing".

### Small classes with injected time

```python
    def __init__(
        self, clock: Callable[[], float], ratio: float = 0.1, window_seconds: float = 10.0, min_retries: int = 3
    ) -> None:
        self._clock = clock
        self._ratio = ratio
        self._window = window_seconds
        self._min_retries = min_retries
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()

    def _trim(self, now: float) -> None:
        for events in (self._requests, self._retries):
            while events and events[0] <= now - self._window:
                events.popleft()

    def record_request(self) -> None:
        """Count one first attempt (not a retry)."""
        now = self._clock()
        self._trim(now)
        self._requests.append(now)

    def try_acquire_retry(self) -> bool:
        """Spend one retry if the budget allows; return whether it was granted."""
        now = self._clock()
        self._trim(now)
        allowed = max(self._min_retries, int(self._ratio * len(self._requests)))
        if len(self._retries) >= allowed:
            return False
        self._retries.append(now)
        return True
```

`clock: Callable[[], float]` is "a function that takes nothing and returns a float". The class never calls `time.time()` itself; the caller passes the clock. A test passes a function that returns a number it controls. `deque` (from `collections`) is a list that is fast to remove from at the front: old events are dropped with `popleft()`. `max(self._min_retries, int(self._ratio * len(self._requests)))` computes how many retries the window allows. `@property` (lines 48 to 58) makes a method readable like an attribute: `budget.retries_in_window`.

### Enumerations

```python
class Profile(StrEnum):
    """What each service does about failure.

    - ``NAIVE``: every client retries three times, immediately, with a 2 second timeout. The lesson's starting point.
    - ``DEADLINE_RETRY``: the gateway sets a 2 second deadline and is the only layer that retries, with backoff,
      jitter and a 10 percent retry budget; every other hop makes one attempt inside what is left of the deadline and
      answers with a degraded result instead of an error.
    - ``FULL``: as above, plus a circuit breaker on pricing's call to the stock store and load shedding at search.
    """

    NAIVE = "naive"
    DEADLINE_RETRY = "deadline-retry"
    FULL = "full"

```

`StrEnum` makes named values that are also strings: `Profile.FULL == "full"` is true. `IntEnum` (in `breaker.py`) makes named values that are also integers, which a metrics gauge can export.

### Generic async functions

```python
async def run_naive[T](attempt: Callable[[float], Awaitable[T]], *, attempts: int = 3, timeout: float = 2.0) -> T:
    """The chain's starting point: try again immediately on any error, with a fixed timeout and no backoff.

    Used at every hop of the naive chain, which is exactly why a slow bottom multiplies into a storm.
    """
    last = AttemptFailed("no attempt made")
    for _ in range(attempts):
        try:
            return await attempt(timeout)
        except AttemptFailed as error:
            last = error
    raise RetriesExhausted(attempts, last, "attempts used up")
```

`async def run_naive[T](...)` is Python 3.12 syntax for "works for any result type `T`". `Callable[[float], Awaitable[T]]` is the type of a function taking a float and returning something you can `await` that yields a `T` (see `asyncio.md`). The `*` in the parameter list means everything after it must be passed by name. `for _ in range(attempts)` loops without needing the counter. `try`/`except AttemptFailed as error` catches one error type; `raise RetriesExhausted(...)` throws a new one when the loop ends.

### Context variables

```python
"""The deadline of the request being handled, set by the middleware from ``X-Deadline-Ms`` and read by the use cases."""

from __future__ import annotations

import contextvars

from resilience.deadline import Deadline

_current: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar("deadline", default=None)


def current_deadline() -> Deadline | None:
    """The caller's deadline, or ``None`` when the caller sent none (the naive chain)."""
    return _current.get()


def set_deadline(deadline: Deadline | None) -> contextvars.Token[Deadline | None]:
    """Make ``deadline`` current; returns the token to restore the previous value."""
    return _current.set(deadline)


def reset_deadline(token: contextvars.Token[Deadline | None]) -> None:
    """Restore the previous deadline."""
    _current.reset(token)
```

A `contextvars.ContextVar` holds a value that is separate for each request being handled at the same time, even though they all run on one thread. The middleware sets the caller's deadline at the start of the request; code deep inside the call tree reads it with `current_deadline()`. `set` returns a **token** that `reset` uses to put the previous value back.

### Regular expressions and tuples

```python
TRACEPARENT = "traceparent"
FORCE_HEADER = "X-Trace-Force"
_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True, slots=True)
class SpanContext:
    """Who a span is: its trace, its own id, and whether the trace is being recorded."""

    trace_id: str
    span_id: str
    sampled: bool

    def header(self) -> str:
        """The ``traceparent`` value to send to the next hop."""
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"


def parse_traceparent(value: str | None) -> SpanContext | None:
    """Parse a ``traceparent`` header; return ``None`` when it is missing or malformed."""
    if value is None:
        return None
    match = _PATTERN.match(value.strip())
    if match is None or set(match.group(1)) == {"0"}:
        return None
    return SpanContext(match.group(1), match.group(2), bool(int(match.group(3), 16) & 1))


def new_trace(sampled: bool) -> SpanContext:
    """Start a trace."""
    return SpanContext(secrets.token_hex(16), secrets.token_hex(8), sampled)


def child_of(parent: SpanContext) -> SpanContext:
    """A new span in the same trace."""
    return SpanContext(parent.trace_id, secrets.token_hex(8), parent.sampled)
```

`re.compile(...)` builds a regular expression: `^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$` matches the W3C `traceparent` header (version `00`, a 32-hex trace id, a 16-hex span id, two hex flags). `secrets.token_hex(16)` makes 16 random bytes as 32 hex characters. f-strings build the header text.

### Exceptions as the language of the policy

```python
class AttemptFailed(Exception):
    """One attempt failed. ``retryable`` says whether trying again can help; ``retry_after`` is the server's hint."""

    def __init__(self, reason: str, *, retryable: bool = True, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.retry_after = retry_after


class RetriesExhausted(Exception):
    """Every allowed attempt failed, or the deadline or the retry budget stopped further attempts."""

    def __init__(self, attempts: int, last: AttemptFailed, why: str) -> None:
        super().__init__(f"gave up after {attempts} attempt(s): {why} (last: {last.reason})")
        self.attempts = attempts
        self.last = last
        self.why = why
```

`class AttemptFailed(Exception)` is the error one try raises, carrying data (`retryable`, `retry_after`). `super().__init__(reason)` runs the parent's constructor so the message shows when the error is printed. Keyword-only parameters after `*` (`retryable: bool = True`) make calls readable: `AttemptFailed("timeout", retryable=False)`.

## Every Python file, one by one: the shared library `chassis`

Before the files, a few words that the explanations use all the time.

- A **statement** is one instruction. A **block** is a group of statements indented under a line that ends with a colon (`if ...:`, `def ...:`, `class ...:`). Indentation is how Python knows where a block ends: four spaces here.
- A **function** is a named block you can run by writing its name with parentheses, `name(...)`. The values in the parentheses are the **arguments**. Inside the function they are called **parameters**. `return value` hands a result back.
- A **method** is a function inside a class. Its first parameter is `self`, the object it is working on.
- A **docstring** is a string written as the first thing in a file, class or function. It documents it. Python keeps it, and tools show it.
- A **constant** is a name written in capitals by convention (`CORRELATION_HEADER`), meaning "do not change this".
- A name that starts with an underscore (`_current`, `_log`) is private by convention: other files should not use it.
- `import x` loads another file so you can use its names. `from x import y` loads it and gives you just `y`.
- A **type hint** after a colon (`value: int`) or after `->` (`-> str`) says what kind of value is expected. Python does not enforce it (see `mypy.md`).

### `libs/chassis/src/chassis/correlation.py`

```python
"""The correlation id that follows one request across every service and every log line."""

from __future__ import annotations

import contextvars
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

CORRELATION_HEADER = "X-Correlation-Id"

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)

```

- `import contextvars`, `re`, `uuid` load three parts of Python's standard library: context variables, regular expressions and unique ids. `from collections.abc import Iterator` and `from contextlib import contextmanager` load a type name and a helper.
- `CORRELATION_HEADER = "X-Correlation-Id"` is a constant: the name of the HTTP header that carries the id between services.
- `_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")` builds a **regular expression**, a small pattern language for text. `^` is the start, `$` the end, `[A-Za-z0-9._-]` means one character that is a letter, a digit, a dot, an underscore or a hyphen, and `{1,64}` means between 1 and 64 of them. The `r` before the quote makes it a raw string, so backslashes are left alone.
- `_current = contextvars.ContextVar("correlation_id", default=None)` creates a **context variable**: a box whose content is separate for each request being handled, even when many requests share one thread. Its type, `contextvars.ContextVar[str | None]`, says the box holds text or nothing (`None`).

```python
def new_correlation_id() -> str:
    """Return a fresh 32-character hexadecimal id."""
    return uuid.uuid4().hex


def sanitize_correlation_id(candidate: str | None) -> str:
    """Accept an incoming id only if it is short and printable-safe; otherwise mint a new one.

    The value ends up in log lines, so anything that could forge a log entry is replaced.
    """
    if candidate is not None and _SAFE_ID.match(candidate):
        return candidate
    return new_correlation_id()


def current_correlation_id() -> str | None:
    """Return the id of the request being handled in this context, if any."""
    return _current.get()
```

- `uuid.uuid4().hex` makes a random 128-bit identifier written as 32 hexadecimal characters.
- `sanitize_correlation_id` accepts a candidate only if it matches the pattern. The docstring gives the reason: the id ends up in log lines, and an id containing a newline could forge a log entry. `_SAFE_ID.match(candidate)` returns a match object (true) or `None` (false).
- `current_correlation_id` reads the box with `.get()`.

```python
@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Make ``correlation_id`` (or a new one) current for the duration of the block."""
    value = sanitize_correlation_id(correlation_id)
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)
```

- `@contextmanager` is a **decorator**: a line starting with `@` that wraps the function below it. This one turns a generator function into something usable in a `with` block.
- `token = _current.set(value)` puts the id in the box and returns a token, a receipt for the old content. `yield value` pauses here and hands the id to the code inside the `with` block. When that block ends, even by an error, `finally:` runs and `_current.reset(token)` puts the old content back.


### `libs/chassis/src/chassis/settings.py`

The twelve-factor idea: a program reads its configuration from **environment variables**. An environment variable is a named text value that the operating system gives to each program when it starts (for example `PORT=8080`). Docker sets them from the compose file.

```python
"""Twelve-factor configuration: typed reads from an environment mapping with every problem reported at once."""

from __future__ import annotations

from collections.abc import Mapping

from chassis.errors import ConfigError

```

`Mapping` is a type for "a read-only dictionary". A **dictionary** (`dict`) stores values under names, for example `{"PORT": "8080"}`. `os.environ` is one.

```python
class EnvReader:
    """Reads typed values from a mapping and remembers every invalid or missing one.

    Call :meth:`finish` after reading everything: it raises one :class:`ConfigError` that lists all
    problems, so a misconfigured container fails once with a complete message instead of one variable at a time.
    """

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ
        self._problems: list[str] = []

```

- `EnvReader` is a class that reads typed values. `__init__(self, environ)` stores the mapping in `self._environ` and starts an empty list `self._problems`, annotated `list[str]` ("a list of text").
- The docstring explains the point: remember every problem and report them together.

```python
    def text(self, name: str, default: str | None = None) -> str:
        """Return the variable, the default, or record that a required variable is missing."""
        value = self._environ.get(name)
        if value is None or value == "":
            if default is None:
                self._problems.append(f"{name} is required")
                return ""
            return default
        return value
```

- `def text(self, name: str, default: str | None = None) -> str:` has a parameter with a **default**: if the caller omits `default`, it is `None`. `str | None` means text or nothing.
- `self._environ.get(name)` looks the name up and returns `None` if it is absent (an index `[name]` would raise an error instead).
- `if value is None or value == "":` treats a missing and an empty variable the same.
- If there is no default, the problem is recorded (`self._problems.append(...)`) and an empty string is returned so reading can continue; otherwise the default is returned. A set variable is returned as it is.

```python
    def integer(self, name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
        """Return an integer within bounds, or the default when unset."""
        raw = self._environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            self._problems.append(f"{name} must be an integer, got {raw!r}")
            return default
        if value < minimum or (maximum is not None and value > maximum):
            self._problems.append(
                f"{name} must be between {minimum} and {maximum if maximum is not None else 'infinity'}"
            )
            return default
        return value
```

- `*, minimum: int = 0, maximum: int | None = None`: the bare `*` means the parameters after it can only be passed by name, `integer("PORT", 8080, minimum=1)`.
- `int(raw)` converts text to a whole number and raises `ValueError` if it cannot; `try`/`except ValueError` catches that and records the problem together with `{raw!r}`, which shows the text with quotes (the `!r` asks for its `repr`, the programmer's view of a value).
- `value < minimum or (maximum is not None and value > maximum)` is the range check. The message uses a **conditional expression**: `maximum if maximum is not None else 'infinity'` picks one of two values.

```python
    def number(self, name: str, default: float, *, minimum: float = 0.0, maximum: float | None = None) -> float:
        """Return a float within bounds, or the default when unset."""
        raw = self._environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = float(raw)
        except ValueError:
            self._problems.append(f"{name} must be a number, got {raw!r}")
            return default
        if value < minimum or (maximum is not None and value > maximum):
            self._problems.append(
                f"{name} must be between {minimum} and {maximum if maximum is not None else 'infinity'}"
            )
            return default
        return value
```

`number` reads a `float` (a number with a decimal point such as `2.0`) with `float(raw)` instead of `int(raw)`. If the text is not a number it records `"<NAME> must be a number, got '<text>'"`; if the value is outside the minimum or maximum it records the range message; otherwise it returns the value. On any problem it returns the default so that reading can continue.

```python
    def choice(self, name: str, default: str, allowed: tuple[str, ...]) -> str:
        """Return one of the allowed strings, or the default when unset."""
        value = self._environ.get(name) or default
        if value not in allowed:
            self._problems.append(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
            return default
        return value

    def finish(self) -> None:
        """Raise :class:`ConfigError` listing every recorded problem, if there are any."""
        if self._problems:
            raise ConfigError("invalid configuration: " + "; ".join(self._problems))
```

- `choice` accepts only a value from a fixed list: `allowed: tuple[str, ...]` is a **tuple** (a fixed-size list) of any number of texts. `self._environ.get(name) or default` uses `or` to fall back to the default when the variable is unset or empty.
- `', '.join(allowed)` glues the items together with a comma and space.
- `finish` raises `ConfigError("invalid configuration: " + "; ".join(self._problems))` if any problem was recorded: one error that lists them all, so a misconfigured container fails once with a full message.


### `libs/chassis/src/chassis/logging.py`

A **log** is a record of what a program did, one line per event. **Structured** logging means each line is machine-readable (here, JSON, a text format for data; see `json.md`).

```python
"""Structured JSON logging: one object per line, the correlation id on every line, secrets masked."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import IO, Any

from chassis.correlation import current_correlation_id

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}
_SECRET_KEYS = ("password", "secret", "token", "authorization", "api_key", "dsn")

```

- `import json, logging, sys, time` load the standard library parts for JSON, logging, the standard streams and time. `from typing import IO, Any` loads two type names: `IO[str]` means "a text stream" and `Any` means "any value".
- `_STANDARD = set(logging.LogRecord(...).__dict__) | {...}` builds a **set** (a collection without duplicates) of the attribute names that every log record already has (`name`, `levelno`, ...), plus four more. Anything not in this set is an "extra" field a caller added. `|` joins two sets.
- `_SECRET_KEYS = (...)` is a tuple of words that suggest a secret.

```python
def _mask(key: str, value: Any) -> Any:
    lowered = key.lower()
    return "***" if any(part in lowered for part in _SECRET_KEYS) else value

```

`_mask` returns `"***"` when the field name contains any secret word, and the original value otherwise. `lowered = key.lower()` makes the comparison case-insensitive; `any(part in lowered for part in _SECRET_KEYS)` is a **generator expression** that asks "is at least one of these words inside the name?".

```python
class JsonFormatter(logging.Formatter):
    """Formats records as single-line JSON with ``ts``, ``level``, ``service``, ``logger``, ``msg``.

    Extra fields passed with ``logger.info("msg", extra={...})`` become top-level keys; keys that look
    like secrets are masked.
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        """Render one record."""
        seconds = int(record.created)
        stamp = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{int((record.created - seconds) * 1000):03d}Z"
        )
```

- `class JsonFormatter(logging.Formatter)` extends the standard `Formatter`, the object that turns a log record into text. `super().__init__()` runs the parent's set-up; the service name is stored.
- `format` overrides the parent's method. `record.created` is the record's time as seconds since 1970 (a float). `int(record.created)` drops the fraction; `time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))` writes it as year-month-dayThour:minute:second in UTC, and the f-string appends milliseconds (`:03d` pads to three digits) and `Z` (for UTC).

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

- `payload` is a dictionary with the fixed keys `ts`, `level`, `service`, `logger`, `msg`. `record.getMessage()` is the message with its values filled in.
- If a correlation id is current, it is added.
- `for key, value in record.__dict__.items():` loops over every attribute of the record. The extras (anything not in `_STANDARD` and not starting with `_`) are added after being passed through `_mask`.
- `if record.exc_info:` adds the formatted traceback (the list of calls that led to an error) under `exc`.
- `json.dumps(payload, default=str, separators=(",", ":"))` converts the dictionary to compact JSON text; `default=str` turns values JSON cannot hold (a time) into text.

```python
def configure_logging(service: str, level: str = "INFO", stream: IO[str] | None = None) -> None:
    """Send every log record of the process, including uvicorn's, to ``stream`` (stderr) as JSON."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").disabled = True
```

`configure_logging` sets up the whole process. `logging.StreamHandler(stream or sys.stderr)` sends records to standard error (`stderr`), the stream programs use for diagnostics; `stream or sys.stderr` uses the argument if given. `root.handlers[:] = [handler]` replaces all handlers with ours. `root.setLevel(level.upper())` sets the lowest level to show (`INFO`, `WARNING`). The last line silences uvicorn's own access log, because the middleware writes a richer one.


### `libs/chassis/src/chassis/health.py`

A **health check** is a way for a platform to ask a program "are you working?". **Liveness** means the process runs; **readiness** means it can do its work (its dependencies answer).

```python
"""Health checks: liveness says the process runs; readiness says its dependencies answer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

CheckFn = Callable[[], None]


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The outcome of running every readiness check."""

    ready: bool
    checks: dict[str, str] = field(default_factory=dict)


class HealthRegistry:
    """Named readiness checks. A check raises when its dependency is unreachable."""

    def __init__(self) -> None:
        self._checks: dict[str, CheckFn] = {}

    def add(self, name: str, check: CheckFn) -> None:
        """Register a check under ``name``."""
        self._checks[name] = check

    def run(self) -> HealthReport:
        """Run every check; a raised exception marks that dependency as failed."""
        results: dict[str, str] = {}
        for name, check in self._checks.items():
            try:
                check()
                results[name] = "ok"
            except Exception as error:
                results[name] = f"failed: {type(error).__name__}"
        return HealthReport(ready=all(value == "ok" for value in results.values()), checks=results)
```

- `CheckFn = Callable[[], None]` gives a short name to a type: a function with no parameters that returns nothing. A check signals trouble by raising an error.
- `HealthReport` is a frozen dataclass with `ready: bool` and `checks: dict[str, str] = field(default_factory=dict)`. `field(default_factory=dict)` makes a new empty dictionary for each report. A plain `= {}` would share one dictionary between all reports, a classic mistake.
- `HealthRegistry` keeps named checks in `self._checks`. `add` registers one. `run` calls each in turn inside `try`/`except Exception`: success records `"ok"`, an exception records `"failed: <ErrorName>"`. `all(value == "ok" for value in results.values())` is true only if every check passed.

### `libs/chassis/src/chassis/__init__.py`, `errors.py`, `web.py`

```python
"""Service chassis shared by the four services: everything that is infrastructure and not policy.

Settings, logging, trace context, metrics, one-attempt upstream calls, health and the database pool live here. The
decisions (when to retry, when to open a breaker) live in the ``resilience`` package and in each service.
"""
```

A file named `__init__.py` makes its folder a **package**: Python can now `import chassis`. This one holds only a docstring that says what lives here (infrastructure) and what does not (decisions): the policy is in `resilience` and in each service. Every other `__init__.py` in the repository holds one docstring line.

```python
"""Errors raised by the chassis."""

from __future__ import annotations


class ConfigError(Exception):
    """The environment does not describe a valid configuration; the message lists every problem."""
```

`ConfigError` is a new kind of error: `class ConfigError(Exception)` inherits from `Exception` and adds only a name, so code can catch exactly "the configuration is wrong". The message lists every problem (see `EnvReader.finish` above).

```python
"""Health routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from chassis.health import HealthRegistry


def health_router(registry: HealthRegistry) -> APIRouter:
    """Routes ``/health/live`` (always 200) and ``/health/ready`` (503 while a dependency is down)."""
    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "up"}

    @router.get("/health/ready")
    async def ready(response: Response) -> dict[str, Any]:
        report = registry.run()
        if not report.ready:
            response.status_code = 503
        return {"status": "ready" if report.ready else "not ready", "checks": report.checks}

    return router
```

`health_router()` returns a group of routes. The routes here are `async def`: an **asynchronous function** can pause at `await` so the server works on other requests meanwhile (see `asyncio.md`). `live` returns a dictionary that FastAPI turns into JSON. `ready` receives a `Response` object so it can change the status code: if the registry's report is not ready it sets 503. The return value includes the per-check results.

### `libs/chassis/src/chassis/postgres.py`

An **asynchronous connection pool** keeps a few database connections open for reuse, and gives them out to coroutines without blocking (see `psycopg.md`).

```python
"""Async PostgreSQL pool with timeouts on everything, opened with retries while the database starts."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import cast

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

_log = logging.getLogger(__name__)

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
_SESSION_OPTIONS = "-c statement_timeout=3000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000"
```

`AsyncConnection` and `AsyncConnectionPool` are the asynchronous twins of the synchronous classes. `Pool = AsyncConnectionPool[AsyncConnection[DictRow]]` names a long type. `_SESSION_OPTIONS` is a text of three PostgreSQL settings: statements stop after 3000 ms, waiting for a lock after 1000 ms, an idle transaction after 10000 ms.

```python
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

`async def create_pool(dsn, *, name, min_size=2, max_size=16, wait_seconds=60.0)` builds a pool and retries until the database answers. `deadline = time.monotonic() + wait_seconds` is the time to give up (`time.monotonic()` is a clock that never goes backwards). `AsyncConnectionPool(..., open=False, ...)` makes the pool without connecting; `check=AsyncConnectionPool.check_connection` makes it test a connection before use; `kwargs` are passed to each connection (dictionary rows, 5-second connect timeout, the session options). `await pool.open(wait=True, timeout=3.0)` connects and waits up to 3 seconds for the minimum number of connections. On `PoolTimeout` it closes the pool, re-raises if the deadline has passed (bare `raise`), otherwise logs a warning and waits `min(2.0, 0.2 * 2**attempt)` seconds with `await asyncio.sleep(...)` before trying again.

```python
async def check_pool(pool: Pool) -> None:
    """Readiness check: run ``SELECT 1`` on a pooled connection."""
    async with pool.connection() as connection:
        await connection.execute("SELECT 1")
```

The readiness check: borrow a connection (`async with` returns it afterwards) and run `SELECT 1`.

### `libs/chassis/src/chassis/deadline_ctx.py`

```python
"""The deadline of the request being handled, set by the middleware from ``X-Deadline-Ms`` and read by the use cases."""

from __future__ import annotations

import contextvars

from resilience.deadline import Deadline

_current: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar("deadline", default=None)


def current_deadline() -> Deadline | None:
    """The caller's deadline, or ``None`` when the caller sent none (the naive chain)."""
    return _current.get()


def set_deadline(deadline: Deadline | None) -> contextvars.Token[Deadline | None]:
    """Make ``deadline`` current; returns the token to restore the previous value."""
    return _current.set(deadline)


def reset_deadline(token: contextvars.Token[Deadline | None]) -> None:
    """Restore the previous deadline."""
    _current.reset(token)
```

A **context variable** is a box whose content is separate for each request being handled, even when many share one thread. `_current` holds the caller's deadline or `None`. `current_deadline()` reads the box, `set_deadline(deadline)` fills it and returns a *token* (a receipt for the old content), and `reset_deadline(token)` puts the old content back. The type `contextvars.Token[Deadline | None]` names that receipt.

### `libs/chassis/src/chassis/tracing.py`

A **trace** is the record of one request's path through several services. A **span** is one step of it: a request served here, or a call made from here. The **W3C `traceparent` header** is the standard way to pass a trace from one service to the next.

```python
"""Trace context in the W3C ``traceparent`` format, and spans kept in memory for the sampled traces."""

from __future__ import annotations

import contextvars
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TRACEPARENT = "traceparent"
FORCE_HEADER = "X-Trace-Force"
_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

```

Imports: `contextvars`, `re` (regular expressions), `secrets` (random values fit for ids), `time`, `OrderedDict` (a dictionary that remembers insertion order), `dataclass` and `field`. `TRACEPARENT` is the header name; `FORCE_HEADER` forces a trace to be recorded; `_PATTERN` is a regular expression that matches `00-<32 hex>-<16 hex>-<2 hex>`, with groups in parentheses for the three parts.

```python
@dataclass(frozen=True, slots=True)
class SpanContext:
    """Who a span is: its trace, its own id, and whether the trace is being recorded."""

    trace_id: str
    span_id: str
    sampled: bool

    def header(self) -> str:
        """The ``traceparent`` value to send to the next hop."""
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"


def parse_traceparent(value: str | None) -> SpanContext | None:
    """Parse a ``traceparent`` header; return ``None`` when it is missing or malformed."""
    if value is None:
        return None
    match = _PATTERN.match(value.strip())
    if match is None or set(match.group(1)) == {"0"}:
        return None
    return SpanContext(match.group(1), match.group(2), bool(int(match.group(3), 16) & 1))


def new_trace(sampled: bool) -> SpanContext:
    """Start a trace."""
    return SpanContext(secrets.token_hex(16), secrets.token_hex(8), sampled)


def child_of(parent: SpanContext) -> SpanContext:
    """A new span in the same trace."""
    return SpanContext(parent.trace_id, secrets.token_hex(8), parent.sampled)

```

`SpanContext` (trace id, span id, whether the trace is recorded) has `header()`, which builds the header text with an f-string (`'01' if self.sampled else '00'` is a conditional expression). `parse_traceparent` returns `None` for a missing or malformed value (`match is None`) or an all-zero trace id (`set(match.group(1)) == {"0"}`) and otherwise builds a context; `bool(int(match.group(3), 16) & 1)` reads the flag byte as a number in base 16 and tests its lowest bit with `&`. `new_trace` makes random ids with `secrets.token_hex(16)` (16 random bytes as 32 hex characters). `child_of` makes a new span in the same trace.

```python
@dataclass(frozen=True, slots=True)
class Span:
    """One finished unit of work: a request served here, or a call made from here."""

    trace_id: str
    span_id: str
    parent_id: str | None
    service: str
    name: str
    start_ms: float
    duration_ms: float
    status: str
    attributes: dict[str, Any] = field(default_factory=dict)

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

`Span` is a finished span (ids, service, name, start and duration in milliseconds, status, attributes). `field(default_factory=dict)` gives each span its own dictionary. `as_dict` renders it with the camel-case names used in JSON.

```python
class SpanStore:
    """Keeps the spans of the most recent ``max_traces`` sampled traces, so memory stays bounded under load."""

    def __init__(self, max_traces: int = 300, max_spans_per_trace: int = 500) -> None:
        self._traces: OrderedDict[str, list[Span]] = OrderedDict()
        self._max_traces = max_traces
        self._max_spans = max_spans_per_trace

    def record(self, span: Span) -> None:
        """Store a span of a sampled trace."""
        spans = self._traces.get(span.trace_id)
        if spans is None:
            spans = self._traces[span.trace_id] = []
            while len(self._traces) > self._max_traces:
                self._traces.popitem(last=False)
        if len(spans) < self._max_spans:
            spans.append(span)

    def trace(self, trace_id: str) -> list[Span]:
        """The spans stored for the trace, oldest first."""
        return sorted(self._traces.get(trace_id, []), key=lambda s: s.start_ms)

    def trace_ids(self) -> list[str]:
        """Stored trace ids, oldest first."""
        return list(self._traces)
```

`SpanStore` keeps the spans of the most recent traces so memory stays bounded. `record` finds the list for the trace id (`self._traces.get(...)`), creates it if needed (the chained assignment `spans = self._traces[span.trace_id] = []` sets both) and drops the oldest trace when there are too many (`popitem(last=False)` removes the first item); a trace keeps at most `max_spans_per_trace` spans. `trace` returns a trace's spans sorted by start (`key=lambda s: s.start_ms`). `trace_ids` lists the ids.

```python
_current: contextvars.ContextVar[SpanContext | None] = contextvars.ContextVar("span_context", default=None)


def current_span() -> SpanContext | None:
    """The span being handled in this task, if any."""
    return _current.get()


def set_current(context: SpanContext | None) -> contextvars.Token[SpanContext | None]:
    """Make ``context`` current; returns the token to restore the previous value."""
    return _current.set(context)


def reset_current(token: contextvars.Token[SpanContext | None]) -> None:
    """Restore the previous span."""
    _current.reset(token)
```

Another context variable holds the span being handled; `current_span`, `set_current` and `reset_current` read, set and restore it, like the deadline's.

```python
class SpanRecorder:
    """Times a block and records a span for it when the trace is sampled."""

    def __init__(
        self,
        store: SpanStore,
        service: str,
        name: str,
        context: SpanContext,
        parent_id: str | None,
        attributes: dict[str, Any] | None = None,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.status = "ok"
        self._store = store
        self._service = service
        self._name = name
        self._parent_id = parent_id
        self._wall = wall
        self._mono = mono
        self._start_wall = 0.0
        self._start_mono = 0.0

    def __enter__(self) -> SpanRecorder:
        self._start_wall = self._wall()
        self._start_mono = self._mono()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc is not None and self.status == "ok":
            self.status = type(exc).__name__
        if self.context.sampled:
            self._store.record(
                Span(
                    self.context.trace_id,
                    self.context.span_id,
                    self._parent_id,
                    self._service,
                    self._name,
                    self._start_wall * 1000.0,
                    (self._mono() - self._start_mono) * 1000.0,
                    self.status,
                    self.attributes,
                )
            )
```

`SpanRecorder` times a block of code and records a span when the trace is sampled. It takes two clocks, `wall` (`time.time`, calendar time) and `mono` (`time.monotonic`, for durations). `__enter__` notes the start times and returns the recorder (so `with ... as span` gives access to `span.status` and `span.attributes`). `__exit__` runs when the block ends: if an error happened and the status is still `ok`, the status becomes the error's class name (`type(exc).__name__`); if the context is sampled, a `Span` is stored with start in milliseconds and duration `(self._mono() - self._start_mono) * 1000.0`.

### `libs/chassis/src/chassis/metrics.py`

```python
"""Prometheus metrics for one service: the counters the lesson reads amplification from, and a ``/metrics`` route."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest


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


def metrics_router(metrics: ServiceMetrics) -> APIRouter:
    """``GET /metrics`` in the Prometheus text format."""
    router = APIRouter(tags=["metrics"])

    @router.get("/metrics")
    async def scrape() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return router
```

`ServiceMetrics` builds a private `CollectorRegistry` and four common metrics: a counter of requests received (labeled by route), a counter of attempts sent (labeled by target), a gauge of requests in flight and a histogram of request durations with the bucket limits listed. `counter()` and `gauge()` let a service add its own to the same registry. `metrics_router` returns the `GET /metrics` route, whose handler calls `generate_latest(registry)` and returns it as a `Response` with the content type Prometheus expects (see `prometheus-client.md`).

### `libs/chassis/src/chassis/observability.py`

```python
"""One ASGI middleware for the cross-cutting concerns of a request: trace context, correlation id, deadline, metrics."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chassis.correlation import correlation_scope
from chassis.deadline_ctx import reset_deadline, set_deadline
from chassis.metrics import ServiceMetrics
from chassis.tracing import (
    FORCE_HEADER,
    TRACEPARENT,
    SpanRecorder,
    SpanStore,
    child_of,
    new_trace,
    parse_traceparent,
    reset_current,
    set_current,
)
from resilience.deadline import DEADLINE_HEADER, Deadline

_log = logging.getLogger("access")
_QUIET_PREFIXES = ("/health", "/metrics", "/admin")

```

`ObservabilityMiddleware` wraps every request of a service. `_QUIET_PREFIXES = ("/health", "/metrics", "/admin")` lists the paths it ignores: probes and control routes are not business requests and must not distort the counters. `path.startswith(_QUIET_PREFIXES)` accepts a tuple of prefixes.

```python
class ObservabilityMiddleware:
    """For each business request: join or start a trace, make the trace id the correlation id, read the deadline header,
    count the request, time it, and record a server span when the trace is sampled.

    ``sample_rate`` only matters where a trace starts (the gateway); other services follow the caller's decision.
    A request with the ``X-Trace-Force`` header is always sampled.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str,
        metrics: ServiceMetrics,
        store: SpanStore,
        sample_rate: float = 0.0,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._service = service
        self._metrics = metrics
        self._store = store
        self._sample_rate = sample_rate
        self._rand = rand
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        path = scope["path"] if scope["type"] == "http" else ""
        if scope["type"] != "http" or path.startswith(_QUIET_PREFIXES):
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        parent = parse_traceparent(headers.get(TRACEPARENT))
        if parent is None:
            sampled = FORCE_HEADER.lower() in headers or self._rand() < self._sample_rate
            context = new_trace(sampled)
            parent_id = None
        else:
            context = child_of(parent)
            parent_id = parent.span_id
        now = self._clock()
        deadline = Deadline.from_header(headers.get(DEADLINE_HEADER.lower()), now)
        route = path
        status = 500
        self._metrics.received.labels(route).inc()
        self._metrics.in_flight.inc()
        token = set_current(context)
        deadline_token = set_deadline(deadline)
        started = self._clock()

```

The constructor stores the wrapped app, the service name, the metrics, the span store, the sample rate and two injectable functions (`rand`, `clock`). `async def __call__(self, scope, receive, send)` is called per request (this is the ASGI interface, see `fastapi.md`). For non-HTTP events and quiet paths it forwards the call at once. Otherwise: it lowercases the header names (`scope["headers"]` is a list of pairs of **bytes**; `.decode("latin-1")` turns bytes into text); `parse_traceparent` reads the caller's trace; with no trace it starts one, sampled if the force header is present or `rand() < sample_rate`, else it joins the caller's trace with a child span. `Deadline.from_header(...)` reads the deadline header. It counts the request, raises the in-flight gauge and stores the context and the deadline in the context variables.

```python
        async def send_with_trace(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-trace-id", context.trace_id.encode()))
            await send(message)

        try:
            with (
                correlation_scope(context.trace_id),
                SpanRecorder(
                    self._store,
                    self._service,
                    f"{self._service} {route}",
                    context,
                    parent_id,
                    {"deadlineMs": None if deadline is None else int(deadline.remaining(now) * 1000)},
                ) as span,
            ):
                try:
                    await self._app(scope, receive, send_with_trace)
                finally:
                    span.status = str(status)
                    if context.sampled or status >= 500:
                        _log.info("request", extra=_fields(scope, status, self._clock() - started))
        finally:
            self._metrics.in_flight.dec()
            self._metrics.duration.labels(route, str(status)).observe(self._clock() - started)
            reset_deadline(deadline_token)
            reset_current(token)


def _fields(scope: Scope, status: int, seconds: float) -> dict[str, Any]:
    return {"method": scope["method"], "path": scope["path"], "status": status, "duration_ms": round(seconds * 1000, 1)}
```

`send_with_trace` wraps `send` so that when the response starts it notes the status and adds an `x-trace-id` header (`nonlocal status` lets the inner function change the outer variable). The `try`/`finally` blocks guarantee clean-up even if the request fails: inside `with correlation_scope(...)` and `SpanRecorder(...)` (a parenthesized `with` with two items) the app runs; the span status is the HTTP status; a log line is written only for sampled traces and for status 500 or above; and the outer `finally` lowers the in-flight gauge, observes the duration in the histogram and restores the deadline and span context. `_fields` builds the log fields.

### `libs/chassis/src/chassis/upstream.py`

```python
"""One attempt at one call to another service: timeout, trace context, deadline header, counters. No retries."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

from chassis.metrics import ServiceMetrics
from chassis.tracing import TRACEPARENT, SpanRecorder, SpanStore, child_of, current_span, new_trace
from resilience.deadline import DEADLINE_HEADER, Deadline
from resilience.retry import AttemptFailed


class HopClient:
    """Calls one downstream service. The caller decides how many times to try; this class makes exactly one attempt.

    A failure is reported as :class:`AttemptFailed` with ``retryable`` set from the outcome: timeouts, connection
    errors and 5xx/429 answers are retryable, other 4xx answers are not. A ``Retry-After`` header becomes
    ``retry_after``.
    """
```

`HopClient` makes exactly **one** attempt at one call to another service; the caller decides whether to try again. Its docstring says how failures map: timeouts, connection errors and 5xx or 429 answers are retryable; other 4xx are not; a `Retry-After` header becomes `retry_after`.

```python
    def __init__(
        self,
        service: str,
        target: str,
        base_url: str,
        *,
        metrics: ServiceMetrics,
        store: SpanStore,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._target = target
        self._metrics = metrics
        self._store = store
        self._clock = clock
        limits = httpx.Limits(max_connections=2000, max_keepalive_connections=200)
        self._client = client or httpx.AsyncClient(base_url=base_url, limits=limits)

    async def call(
        self, path: str, body: dict[str, Any], *, timeout: float, deadline: Deadline | None, attempt: int = 1
    ) -> dict[str, Any]:
        """One POST of ``body``; return the JSON answer or raise :class:`AttemptFailed`."""
        self._metrics.sent.labels(self._target).inc()
```

The constructor takes names, the metrics, the span store, an optional ready-made `client` and a clock. `httpx.Limits(max_connections=2000, max_keepalive_connections=200)` raises the pool size well above the default so the client never becomes the bottleneck being studied.

```python
    async def call(
        self, path: str, body: dict[str, Any], *, timeout: float, deadline: Deadline | None, attempt: int = 1
    ) -> dict[str, Any]:
        """One POST of ``body``; return the JSON answer or raise :class:`AttemptFailed`."""
        self._metrics.sent.labels(self._target).inc()
        parent = current_span() or new_trace(False)
        context = child_of(parent)
        headers = {TRACEPARENT: context.header()}
        if deadline is not None:
            headers[DEADLINE_HEADER] = deadline.header_value(self._clock())
        with SpanRecorder(
            self._store,
            self._service,
            f"call {self._target}",
            context,
            parent.span_id,
            {"attempt": attempt, "timeoutMs": int(timeout * 1000)},
        ) as span:
            try:
                async with asyncio.timeout(timeout):
                    response = await self._client.post(path, json=body, headers=headers)
            except TimeoutError as error:
                span.status = "timeout"
                raise AttemptFailed("timeout") from error
            except httpx.TransportError as error:
                span.status = "connect-error"
                raise AttemptFailed(type(error).__name__) from error
            span.status = str(response.status_code)
            if response.status_code >= 500 or response.status_code == 429:
                raise AttemptFailed(f"status {response.status_code}", retry_after=_retry_after(response))
            if response.status_code >= 400:
                raise AttemptFailed(f"status {response.status_code}", retryable=False)
            answer: dict[str, Any] = response.json()
            return answer
```

`call` counts the attempt (`self._metrics.sent.labels(self._target).inc()`), takes the current span or starts a trace, makes a child span for the call and builds the headers (`TRACEPARENT`, and `X-Deadline-Ms` when there is a deadline). `with SpanRecorder(...) as span:` records the call. `async with asyncio.timeout(timeout):` bounds the request; `TimeoutError` becomes `AttemptFailed("timeout")` and a transport error `AttemptFailed(type(error).__name__)`. A status of 500 or above, or 429, raises a retryable `AttemptFailed` (with `Retry-After` if present); another 4xx raises a non-retryable one; otherwise the JSON answer is returned.

```python
    async def close(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None
```

`close` releases the pool. `_retry_after` reads the `Retry-After` header as seconds and returns `None` if the header is absent (`KeyError`) or not a number (`ValueError`).

### `libs/chassis/src/chassis/admin.py`

```python
"""Admin routes for the experiments: switch the resilience profile, read traces. Bound to localhost by compose."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from chassis.tracing import SpanStore
from resilience.profile import Profile, ProfileHolder


class ProfileBody(BaseModel):
    """``POST /admin/profile``."""

    name: str


def admin_router(holder: ProfileHolder | None, store: SpanStore) -> APIRouter:
    """``/admin/profile`` (when the service has a profile) and ``/admin/traces``.

    These routes change behavior and expose internals. They exist so the experiments can drive the chain;
    the compose file publishes every port on 127.0.0.1 only, and a real deployment would not mount this router.
    """
    router = APIRouter(prefix="/admin", tags=["admin"])

    if holder is not None:

        @router.get("/profile")
        async def read_profile() -> dict[str, str]:
            return {"name": holder.get().value}

        @router.post("/profile")
        async def set_profile(body: ProfileBody) -> dict[str, str]:
            try:
                holder.set(Profile(body.name))
            except ValueError as error:
                raise HTTPException(status_code=422, detail=f"unknown profile {body.name!r}") from error
            return {"name": holder.get().value}

    @router.get("/traces")
    async def list_traces() -> list[str]:
        return store.trace_ids()

    @router.get("/traces/{trace_id}")
    async def read_trace(trace_id: str) -> list[dict[str, Any]]:
        return [span.as_dict() for span in store.trace(trace_id)]

    return router
```

Control routes for the experiments. `ProfileBody` is the body of `POST /admin/profile`. `admin_router(holder, store)` builds a router with prefix `/admin`; if `holder` is not `None` (stock has no profile) it adds `GET` and `POST /admin/profile` (the POST converts the name with `Profile(body.name)` and answers 422 for an unknown name) and always adds `GET /admin/traces` and `GET /admin/traces/{trace_id}`. The docstring warns that these routes change behavior and expose internals, so a real deployment would not mount them.

## Every Python file, one by one: the policy library `resilience`

The library holds the decisions as plain objects. None of them reads a clock or sleeps by itself; each is given what it needs, so a test can control time. `resilience` has no dependencies outside the standard library.

### `libs/resilience/src/resilience/__init__.py` and `errors.py`

```python
"""The resilience policy of the chain, as plain objects with no I/O.

Every class takes its clock (and sleeping, and randomness) as an argument, so each behavior is a unit test with a
fake clock instead of a timing experiment. Services import this package from their domain and application layers.
"""
```

The package docstring states the principle: every class takes its clock (and sleeping, and randomness) as an argument, so each behavior is a unit test with a fake clock instead of a timing experiment.

```python
"""Errors that cross service boundaries in the same way in every service."""

from __future__ import annotations


class DeadlineExceeded(Exception):
    """The request cannot finish inside its deadline, so the service refuses (or abandons) the work."""


class Overloaded(Exception):
    """The service is at its concurrency limit and sheds this request; ``retry_after`` is the hint for the caller."""

    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__("overloaded")
        self.retry_after = retry_after
```

Two errors that cross service boundaries. `DeadlineExceeded` means the request cannot finish inside its deadline. `Overloaded.__init__(self, retry_after: float = 1.0)` stores a hint for the caller (seconds to wait) and sets the message `"overloaded"` through `super().__init__`.

### `libs/resilience/src/resilience/deadline.py`

A **deadline** is the moment by which a whole request must be answered. It travels with the request so each hop knows how much time is left.

```python
"""A deadline travels with a request: each hop gets what is left, never more."""

from __future__ import annotations

from dataclasses import dataclass

DEADLINE_HEADER = "X-Deadline-Ms"


@dataclass(frozen=True, slots=True)
class Deadline:
    """The moment (on a monotonic clock, in seconds) by which the whole request must be answered."""

    expires_at: float

    @classmethod
    def after(cls, now: float, seconds: float) -> Deadline:
        """A deadline ``seconds`` from ``now``."""
        return cls(now + seconds)
```

`DEADLINE_HEADER = "X-Deadline-Ms"` is the HTTP header that carries the remaining milliseconds. `Deadline` is a frozen dataclass of one number, `expires_at`, in seconds on a **monotonic clock** (a clock that only moves forward). `@classmethod after(cls, now, seconds)` is an alternate constructor: `Deadline.after(now, 2.0)` returns a deadline two seconds ahead.

```python
    @classmethod
    def from_header(cls, value: str | None, now: float, ceiling_seconds: float = 60.0) -> Deadline | None:
        """Parse the remaining milliseconds a caller sent; ignore a missing, malformed or negative value.

        A huge value is clamped to ``ceiling_seconds`` so a caller cannot make this service wait forever.
        """
        if value is None:
            return None
        try:
            remaining_ms = float(value)
        except ValueError:
            return None
        if remaining_ms != remaining_ms or remaining_ms < 0:
            return None
        return cls.after(now, min(remaining_ms / 1000.0, ceiling_seconds))
```

`from_header` parses the header defensively. It returns `None` (no deadline) for a missing, malformed or negative value; `remaining_ms != remaining_ms` is true only for NaN ("not a number"), the one value not equal to itself. It clamps a huge value to `ceiling_seconds` so a caller cannot make this service wait for ever, and divides by 1000 to turn milliseconds into seconds.

```python
    def remaining(self, now: float) -> float:
        """Seconds left, never negative."""
        return max(0.0, self.expires_at - now)

    def expired(self, now: float) -> bool:
        """True when nothing is left."""
        return self.expires_at <= now

    def header_value(self, now: float) -> str:
        """The remaining milliseconds, as sent to the next hop."""
        return str(int(self.remaining(now) * 1000))

    def attempt_timeout(self, now: float, hop_limit: float, margin: float) -> float:
        """The timeout for one call: the hop's own limit, or what is left minus ``margin``, whichever is smaller."""
        return max(0.0, min(hop_limit, self.remaining(now) - margin))
```

`remaining(now)` is the time left, never negative (`max(0.0, ...)`). `expired(now)` is true when nothing is left. `header_value(now)` is the remaining milliseconds as text (`int(self.remaining(now) * 1000)`). `attempt_timeout(now, hop_limit, margin)` is the timeout for one call: the smaller of the hop's own limit and the time left minus a safety margin, never below zero.

### `libs/resilience/src/resilience/backoff.py`

```python
"""Exponential backoff with full jitter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Backoff:
    """The pause after failed attempt ``n`` is a random number between 0 and ``min(cap, base * multiplier^(n-1))``.

    Full jitter spreads clients out in time; without it, every client that failed together retries together.
    """

    base: float = 0.05
    cap: float = 0.5
    multiplier: float = 2.0

    def delay(self, attempt: int, rand: float) -> float:
        """Pause after attempt number ``attempt`` (1-based) for a random draw ``rand`` in [0, 1)."""
        return min(self.cap, self.base * self.multiplier ** (attempt - 1)) * rand
```

**Exponential backoff with full jitter.** After failed attempt `n` the pause is a random number between 0 and `min(cap, base * multiplier ** (attempt - 1))`. `self.multiplier ** (attempt - 1)` is a power: with base 0.05 and multiplier 2 the ceilings are 0.05, 0.1, 0.2, 0.4, then capped at 0.5. `* rand` (a number from 0 to just under 1) picks the pause inside the range. The docstring explains why: without jitter every client that failed together retries together.

### `libs/resilience/src/resilience/retry_budget.py`

A **retry budget** caps how much extra load retries may add, however many requests fail.

```python
"""A retry budget: retries may add at most a fixed fraction of load, however many requests fail."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable


class RetryBudget:
    """Allows a retry only while the retries of the last ``window_seconds`` stay under ``ratio`` times the requests.

    ``min_retries`` lets a quiet service retry a little (otherwise 10 percent of 5 requests is zero retries).
    When many requests fail at once, the budget runs out and the rest fail fast instead of piling retries on a
    struggling dependency, which is the point.
    """

    def __init__(
        self, clock: Callable[[], float], ratio: float = 0.1, window_seconds: float = 10.0, min_retries: int = 3
    ) -> None:
        self._clock = clock
        self._ratio = ratio
        self._window = window_seconds
        self._min_retries = min_retries
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()

    def _trim(self, now: float) -> None:
        for events in (self._requests, self._retries):
            while events and events[0] <= now - self._window:
                events.popleft()

```

`from collections import deque` loads a list that is fast to remove from at the front. The constructor stores the clock, `ratio` (0.1: retries may be 10 percent of requests), `window_seconds` (10), `min_retries` (3, so a quiet service may still retry a little: 10 percent of 5 requests is zero) and two deques of timestamps, `_requests` and `_retries`. `_trim(now)` drops events older than the window from both (`for events in (self._requests, self._retries):` loops over the pair).

```python
    def record_request(self) -> None:
        """Count one first attempt (not a retry)."""
        now = self._clock()
        self._trim(now)
        self._requests.append(now)

    def try_acquire_retry(self) -> bool:
        """Spend one retry if the budget allows; return whether it was granted."""
        now = self._clock()
        self._trim(now)
        allowed = max(self._min_retries, int(self._ratio * len(self._requests)))
        if len(self._retries) >= allowed:
            return False
        self._retries.append(now)
        return True

    @property
    def retries_in_window(self) -> int:
        """Retries spent in the current window."""
        self._trim(self._clock())
        return len(self._retries)

    @property
    def requests_in_window(self) -> int:
        """First attempts in the current window."""
        self._trim(self._clock())
        return len(self._requests)
```

`record_request` notes one first attempt. `try_acquire_retry` computes how many retries the window allows, `max(self._min_retries, int(self._ratio * len(self._requests)))`, and refuses (returns `False`) if that many have been spent; otherwise it records one and returns `True`. The two `@property` methods report counts for tests and metrics.

### `libs/resilience/src/resilience/breaker.py`

A **circuit breaker** stops calling a dependency that is failing and tries it again after a rest, like the breaker in a fuse box.

```python
"""A circuit breaker: stop calling a dependency that is failing, and probe it now and then."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from enum import IntEnum


class BreakerState(IntEnum):
    """The numeric value is what the metrics gauge exports."""

    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


class CircuitBreaker:
    """Opens when ``failure_ratio`` of the calls of the last ``window_seconds`` failed (and there were ``min_calls``).

    While open it refuses every call locally, without touching the network, for ``open_seconds``. Then it lets
    ``half_open_probes`` calls through: if they all succeed it closes, and one failure opens it again.
    """

```

`BreakerState` is an `IntEnum` (named values that are also integers: `CLOSED = 0` calls flow, `OPEN = 1` calls are refused, `HALF_OPEN = 2` a few trial calls are allowed); the number is what the metrics gauge exports. The docstring of `CircuitBreaker` states the rule: open when the failure ratio in the last window reaches the threshold and there were at least `min_calls`; while open refuse everything locally for `open_seconds`; then let `half_open_probes` calls through; if all succeed close, if one fails open again.

```python
    def __init__(
        self,
        clock: Callable[[], float],
        failure_ratio: float = 0.5,
        min_calls: int = 10,
        window_seconds: float = 10.0,
        open_seconds: float = 5.0,
        half_open_probes: int = 3,
    ) -> None:
        self._clock = clock
        self._failure_ratio = failure_ratio
        self._min_calls = min_calls
        self._window = window_seconds
        self._open_seconds = open_seconds
        self._probes = half_open_probes
        self._calls: deque[tuple[float, bool]] = deque()
        self._state = BreakerState.CLOSED
        self._opened_at = 0.0
        self._probes_started = 0
        self._probes_succeeded = 0

    @property
    def state(self) -> BreakerState:
        """The current state (an open breaker becomes half-open once its cool-down has passed)."""
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self._open_seconds:
            self._state = BreakerState.HALF_OPEN
            self._probes_started = 0
            self._probes_succeeded = 0
        return self._state

    def allow(self) -> bool:
        """May a call go out now? In half-open state only a limited number of probes may."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        if self._probes_started < self._probes:
            self._probes_started += 1
            return True
        return False
```

The constructor stores the settings and the state: `_calls` is a deque of `(time, ok)` pairs. The `state` property returns the stored state, except that an open breaker whose rest period has passed becomes half-open (and resets the probe counters). `allow()` answers "may a call go out now?": yes when closed, no when open, and in half-open state yes only for the first `_probes` calls.

```python
    def record_success(self) -> None:
        """A call succeeded."""
        if self.state is BreakerState.HALF_OPEN:
            self._probes_succeeded += 1
            if self._probes_succeeded >= self._probes:
                self._state = BreakerState.CLOSED
                self._calls.clear()
            return
        self._add(True)

    def record_failure(self) -> None:
        """A call failed or timed out."""
        if self.state is BreakerState.HALF_OPEN:
            self._open()
            return
        self._add(False)
        failures = sum(1 for _, ok in self._calls if not ok)
        if len(self._calls) >= self._min_calls and failures / len(self._calls) >= self._failure_ratio:
            self._open()

    def _add(self, ok: bool) -> None:
        now = self._clock()
        self._calls.append((now, ok))
        while self._calls and self._calls[0][0] <= now - self._window:
            self._calls.popleft()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._calls.clear()
```

`record_success`: in half-open state it counts a successful probe and closes the breaker (clearing the history) when enough succeeded; otherwise it records a success in the window. `record_failure`: in half-open state one failure opens the breaker again; otherwise it records the failure, counts failures in the window with a generator expression (`sum(1 for _, ok in self._calls if not ok)`; the `_` ignores the time of each pair) and opens when there are at least `min_calls` calls and the ratio is at or above the threshold. `_add` appends and drops entries older than the window; `_open` sets the state, notes when, and clears the history.

### `libs/resilience/src/resilience/limiter.py`

**Load shedding** means refusing work beyond what the service can do, immediately, instead of queueing it.

```python
"""Load shedding: refuse work beyond what the service can do, immediately, instead of queueing it."""

from __future__ import annotations


class ConcurrencyLimiter:
    """At most ``limit`` requests in progress. ``try_acquire`` never waits: a request over the limit is shed."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._in_flight = 0

    @property
    def limit(self) -> int:
        """The maximum number of requests in progress."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """Requests currently holding a slot."""
        return self._in_flight

    def try_acquire(self) -> bool:
        """Take a slot if one is free."""
        if self._in_flight >= self._limit:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        """Give a slot back."""
        if self._in_flight > 0:
            self._in_flight -= 1
```

`ConcurrencyLimiter(limit)` refuses a limit below 1 with `ValueError`. It counts requests in progress (`_in_flight`). `try_acquire()` takes a slot if one is free and never waits: when full it returns `False` at once. `release()` gives a slot back (never below zero). Two `@property` methods read the limit and the count.

### `libs/resilience/src/resilience/profile.py`

```python
"""The three resilience profiles the experiments switch between, at runtime, in every service."""

from __future__ import annotations

from enum import StrEnum


class Profile(StrEnum):
    """What each service does about failure.

    - ``NAIVE``: every client retries three times, immediately, with a 2 second timeout. The lesson's starting point.
    - ``DEADLINE_RETRY``: the gateway sets a 2 second deadline and is the only layer that retries, with backoff,
      jitter and a 10 percent retry budget; every other hop makes one attempt inside what is left of the deadline and
      answers with a degraded result instead of an error.
    - ``FULL``: as above, plus a circuit breaker on pricing's call to the stock store and load shedding at search.
    """

    NAIVE = "naive"
    DEADLINE_RETRY = "deadline-retry"
    FULL = "full"


class ProfileHolder:
    """The profile a service is running with. Replaced as a whole, so a reader never sees a half-changed state."""

    def __init__(self, initial: Profile = Profile.NAIVE) -> None:
        self._profile = initial

    def get(self) -> Profile:
        """The current profile."""
        return self._profile

    def set(self, profile: Profile) -> None:
        """Switch profile."""
        self._profile = profile
```

`Profile` is a `StrEnum` of the three policies the experiments switch between: `naive`, `deadline-retry` and `full`; the docstring describes each (the first is the lesson's starting point, the second adds the gateway-only budgeted retry and deadlines, the third adds a breaker and load shedding). `ProfileHolder` holds the current profile; `set` replaces it as a whole so a reader never sees a half-changed state.

### `libs/resilience/src/resilience/retry.py`

```python
"""Retry loops: the naive one the chain starts with, and the budgeted one only the edge is allowed to use."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from resilience.backoff import Backoff
from resilience.deadline import Deadline
from resilience.retry_budget import RetryBudget


class AttemptFailed(Exception):
    """One attempt failed. ``retryable`` says whether trying again can help; ``retry_after`` is the server's hint."""

    def __init__(self, reason: str, *, retryable: bool = True, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.retry_after = retry_after


class RetriesExhausted(Exception):
    """Every allowed attempt failed, or the deadline or the retry budget stopped further attempts."""

    def __init__(self, attempts: int, last: AttemptFailed, why: str) -> None:
        super().__init__(f"gave up after {attempts} attempt(s): {why} (last: {last.reason})")
        self.attempts = attempts
        self.last = last
        self.why = why

```

Imports `asyncio`, the type names `Awaitable` and `Callable`, and the three policy classes. `AttemptFailed` is the error one try raises, with `retryable` and `retry_after` (keyword-only after `*`). `RetriesExhausted` is raised when the allowed attempts are used up or the deadline or the budget stops further attempts; its message is built from the number of attempts, the reason and the last error.

```python
async def run_naive[T](attempt: Callable[[float], Awaitable[T]], *, attempts: int = 3, timeout: float = 2.0) -> T:
    """The chain's starting point: try again immediately on any error, with a fixed timeout and no backoff.

    Used at every hop of the naive chain, which is exactly why a slow bottom multiplies into a storm.
    """
    last = AttemptFailed("no attempt made")
    for _ in range(attempts):
        try:
            return await attempt(timeout)
        except AttemptFailed as error:
            last = error
    raise RetriesExhausted(attempts, last, "attempts used up")
```

`run_naive` is the starting point: try up to `attempts` times immediately, with a fixed timeout, whatever the error. `async def run_naive[T](...)` is generic (works for any result type `T`). `last = AttemptFailed("no attempt made")` is the value reported if the loop never runs. `for _ in range(attempts):` tries; `return await attempt(timeout)` leaves on the first success; `except AttemptFailed as error: last = error` remembers the failure; after the loop `raise RetriesExhausted(...)`.

```python
async def run_budgeted[T](
    attempt: Callable[[float], Awaitable[T]],
    *,
    deadline: Deadline,
    budget: RetryBudget,
    backoff: Backoff,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float],
    max_attempts: int = 3,
    hop_timeout: float = 1.8,
    margin: float = 0.05,
    min_useful: float = 0.1,
    on_retry: Callable[[int, str], None] | None = None,
) -> T:
    """Try, and retry only if all of these hold: the error is retryable, the budget allows it, enough time is left.

    The pause between attempts is exponential with full jitter (or the server's ``Retry-After`` if it is longer),
    and the pause must itself fit in the remaining time. No attempt gets a timeout beyond what is left of the deadline.
    """
    budget.record_request()
    last = AttemptFailed("no attempt made")
    for number in range(1, max_attempts + 1):
        timeout = deadline.attempt_timeout(clock(), hop_timeout, margin)
        if timeout < min_useful:
            raise RetriesExhausted(number - 1, last, "deadline too short to try")
        try:
            return await attempt(timeout)
        except AttemptFailed as error:
            last = error
        if not last.retryable:
            raise RetriesExhausted(number, last, "error is not retryable")
        if number == max_attempts:
            break
        pause = max(backoff.delay(number, rand()), last.retry_after or 0.0)
        if deadline.remaining(clock()) - pause < min_useful + margin:
            raise RetriesExhausted(number, last, "not enough time left to retry")
        if not budget.try_acquire_retry():
            raise RetriesExhausted(number, last, "retry budget spent")
        if on_retry is not None:
            on_retry(number, last.reason)
        await sleep(pause)
    raise RetriesExhausted(max_attempts, last, "attempts used up")
```

`run_budgeted` is the loop only the gateway is allowed to use. It first notes the request in the budget. For each attempt it asks the deadline for a timeout (`deadline.attempt_timeout(clock(), hop_timeout, margin)`); a timeout below `min_useful` raises `RetriesExhausted(..., "deadline too short to try")`. It runs the attempt. After a failure: a non-retryable error stops (`"error is not retryable"`); the last allowed attempt stops; otherwise the pause is the larger of the jittered backoff and the server's `Retry-After` (`max(backoff.delay(number, rand()), last.retry_after or 0.0)`); if the deadline leaves too little time after the pause it stops (`"not enough time left to retry"`); if the budget refuses it stops (`"retry budget spent"`); otherwise it calls `on_retry` and `await sleep(pause)`. Retry only if the error is retryable, the budget allows it and enough time is left: all three conditions.

## Every Python file, one by one: the four services

The services form a chain, gateway to search to pricing to stock, and each has four layers: `domain` (plain values and rules), `application` (the use case and its ports), `infrastructure` (adapters: HTTP to the next service, settings, telemetry, the database) and `interfaces` (the HTTP API, start-up wiring and the entry point).

### The gateway

```python
"""What the gateway returns."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchAnswer:
    """The result of a search as the client sees it. ``degraded`` is the signal that part of the chain was bypassed."""

    query: str
    sku: str
    price: str | None
    in_stock: bool | None
    degraded: bool
    stale: bool
```

`SearchAnswer` is the result as the client sees it: the query, the sku, a price that may be `None` (`str | None`), whether it is in stock (`bool | None`), `degraded` (part of the chain was bypassed) and `stale` (an earlier answer was used).

```python
"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol

from gateway.domain.models import SearchAnswer
from resilience.deadline import Deadline


class SearchUpstream(Protocol):
    """One attempt at asking search. Raises ``AttemptFailed``."""

    async def search(self, query: str, timeout: float, deadline: Deadline | None, attempt: int) -> SearchAnswer:
        """The answer to the query."""


class Telemetry(Protocol):
    """Counters the experiments read."""

    def retry(self, reason: str) -> None:
        """A retry was issued."""

    def outcome(self, outcome: str) -> None:
        """The request ended as ``ok``, ``degraded`` or ``unavailable``."""
```

Two **protocols** (descriptions of what a part must be able to do). `SearchUpstream.search(...)` is `async def`: it is awaited. `Telemetry` has `retry(reason)` and `outcome(outcome)`.

```python
"""Handle a search request at the edge of the chain."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from gateway.application.ports import SearchUpstream, Telemetry
from gateway.domain.models import SearchAnswer
from resilience.backoff import Backoff
from resilience.deadline import Deadline
from resilience.profile import Profile
from resilience.retry import RetriesExhausted, run_budgeted, run_naive
from resilience.retry_budget import RetryBudget

END_TO_END_DEADLINE = 2.0
HOP_TIMEOUT = 1.8


class Unavailable(Exception):
    """The gateway could not get an answer inside the policy: the client should come back after ``retry_after``."""

    def __init__(self, reason: str, retry_after: float = 1.0) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class HandleSearch:
    """What the gateway does depends on the profile.

    - ``naive``: three immediate attempts with a 2 second timeout each, no deadline passed down.
    - ``deadline-retry`` and ``full``: the request gets a 2 second deadline that travels in a header; the gateway
      retries (up to three attempts) with exponential backoff and full jitter, only while the retry budget (10 percent
      of requests) and the remaining time allow it. No other service retries.
    """
```

Imports of the policy classes. `END_TO_END_DEADLINE = 2.0` seconds and `HOP_TIMEOUT = 1.8`. `Unavailable` carries a `retry_after`. The docstring of `HandleSearch` states the rule: naive means three immediate attempts with no deadline; the other two profiles give the request a 2-second deadline and retry with backoff and jitter only while the 10 percent budget and the remaining time allow, and no other service retries.

```python
    def __init__(
        self,
        search: SearchUpstream,
        profile: Callable[[], Profile],
        budget: RetryBudget,
        backoff: Backoff,
        clock: Callable[[], float],
        telemetry: Telemetry,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._search = search
        self._profile = profile
        self._budget = budget
        self._backoff = backoff
        self._clock = clock
        self._telemetry = telemetry
        self._sleep = sleep
        self._rand = rand

    async def __call__(self, query: str) -> SearchAnswer:
```

The constructor stores the upstream, a `profile` function, the budget, the backoff, a clock, telemetry, a `sleep` and a `rand` (the last two have defaults, `asyncio.sleep` and `random.random`, which tests replace).

```python
    async def __call__(self, query: str) -> SearchAnswer:
        """Return the answer or raise :class:`Unavailable`."""
        counter = {"n": 0}

        async def attempt(timeout: float, deadline: Deadline | None) -> SearchAnswer:
            counter["n"] += 1
            return await self._search.search(query, timeout, deadline, counter["n"])

        try:
            if self._profile() is Profile.NAIVE:
                answer = await run_naive(lambda timeout: attempt(timeout, None))
            else:
                deadline = Deadline.after(self._clock(), END_TO_END_DEADLINE)
                answer = await run_budgeted(
                    lambda timeout: attempt(timeout, deadline),
                    deadline=deadline,
                    budget=self._budget,
                    backoff=self._backoff,
                    clock=self._clock,
                    sleep=self._sleep,
                    rand=self._rand,
                    hop_timeout=HOP_TIMEOUT,
                    on_retry=lambda _n, reason: self._telemetry.retry(reason),
                )
        except RetriesExhausted as error:
            self._telemetry.outcome("unavailable")
            raise Unavailable(error.why) from error
        self._telemetry.outcome("degraded" if answer.degraded else "ok")
        return answer
```

`async def __call__(self, query)`: `counter = {"n": 0}` is a dictionary used as a mutable number so the inner function can change it. `attempt(timeout, deadline)` increments the counter and awaits the upstream with the attempt number. In the `try`: with the naive profile (`self._profile() is Profile.NAIVE`) it runs `run_naive(lambda timeout: attempt(timeout, None))`; otherwise it creates the deadline and runs `run_budgeted(...)` with all the policy objects and `on_retry=lambda _n, reason: self._telemetry.retry(reason)` (a lambda ignoring its first argument). `except RetriesExhausted as error:` records `unavailable` and raises `Unavailable(error.why) from error`. A successful answer records `degraded` or `ok` and is returned.

```python
"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the gateway needs to start."""

    search_url: str
    port: int
    log_level: str
    trace_sample_rate: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            search_url=reader.text("SEARCH_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
            trace_sample_rate=reader.number("TRACE_SAMPLE_RATE", 0.0, minimum=0.0, maximum=1.0),
        )
        reader.finish()
        return settings
```

Settings: `SEARCH_URL` (required), `PORT`, `LOG_LEVEL`, and `TRACE_SAMPLE_RATE` (a number from 0.0 to 1.0).

```python
"""Prometheus counters for the gateway."""

from __future__ import annotations

from chassis.metrics import ServiceMetrics


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

`PrometheusTelemetry` implements the port with two counters. `retry` maps a reason to a label from a fixed set (`reason if reason in {...} else "other"`) so the number of label values stays small. `.labels(label).inc()` adds 1 to the counter of that label.

```python
"""The gateway's HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from chassis.admin import admin_router
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from gateway.application.handle_search import HandleSearch, Unavailable
from resilience.profile import ProfileHolder


class SearchBody(BaseModel):
    """``POST /search``."""

    query: str = Field(min_length=1, max_length=200)


def create_app(
    handle: HandleSearch,
    holder: ProfileHolder,
    health: HealthRegistry,
    metrics: ServiceMetrics,
    store: SpanStore,
    sample_rate: float,
) -> FastAPI:
```

Imports and the request model `SearchBody` (1 to 200 characters). `create_app` receives the use case, the profile holder, the health registry, the metrics, the span store and the sample rate.

```python
    app = FastAPI(title="gateway", docs_url=None, redoc_url=None)
    app.add_middleware(
        ObservabilityMiddleware, service="gateway", metrics=metrics, store=store, sample_rate=sample_rate
    )
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(holder, store))

    @app.post("/search", response_model=None)
    async def search(body: SearchBody) -> dict[str, Any] | JSONResponse:
        try:
            answer = await handle(body.query)
        except Unavailable as error:
            return JSONResponse(
                {"detail": "temporarily unavailable", "reason": str(error)},
                status_code=503,
                headers={"Retry-After": str(int(error.retry_after))},
            )
        return {
            "query": answer.query,
            "sku": answer.sku,
            "price": answer.price,
            "inStock": answer.in_stock,
            "degraded": answer.degraded,
            "stale": answer.stale,
        }

    return app
```

The app, its middleware, the shared routers and `POST /search`, which answers 503 with `Retry-After` when the policy gives up (see `fastapi.md`).

```python
"""The composition root: build adapters, wire the use case, return the application."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from chassis.upstream import HopClient
from gateway.application.handle_search import HandleSearch
from gateway.infrastructure.http_search import HttpSearchUpstream
from gateway.infrastructure.settings import Settings
from gateway.infrastructure.telemetry import PrometheusTelemetry
from gateway.interfaces.api import create_app
from resilience.backoff import Backoff
from resilience.profile import ProfileHolder
from resilience.retry_budget import RetryBudget


def build_app(settings: Settings) -> FastAPI:
    """Wire the use case and close the HTTP client when the app shuts down."""
    metrics = ServiceMetrics("gateway")
    store = SpanStore()
    holder = ProfileHolder()
    client = HopClient("gateway", "search", settings.search_url, metrics=metrics, store=store)
    budget = RetryBudget(time.monotonic, ratio=0.1, window_seconds=10.0, min_retries=3)
    use_case = HandleSearch(
        HttpSearchUpstream(client),
        holder.get,
        budget,
        Backoff(base=0.05, cap=0.5),
        time.monotonic,
        PrometheusTelemetry(metrics),
    )
    app = create_app(use_case, holder, HealthRegistry(), metrics, store, settings.trace_sample_rate)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.close()

    app.router.lifespan_context = lifespan
    return app
```

The composition root: it creates the metrics, the span store and the profile holder; a `HopClient` for search; a `RetryBudget(time.monotonic, ratio=0.1, window_seconds=10.0, min_retries=3)`; the use case with `Backoff(base=0.05, cap=0.5)`; and the app. The lifespan closes the client at shutdown (`await client.close()`).

```python
"""Process entry point: ``gateway-serve``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from chassis.errors import ConfigError
from chassis.logging import configure_logging
from gateway.infrastructure.settings import Settings
from gateway.interfaces.bootstrap import build_app


async def _serve(settings: Settings) -> None:
    app = build_app(settings)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_config=None, timeout_graceful_shutdown=10, backlog=4096
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    """Read the environment, build the application and serve until SIGTERM, then drain in-flight requests."""
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        configure_logging("gateway")
        logging.getLogger("gateway").error(str(error))
        sys.exit(2)
    configure_logging("gateway", settings.log_level)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
```

Starts the server in an event loop with `asyncio.run(_serve(settings))` (see `uvicorn.md`); invalid settings log one JSON line and `sys.exit(2)`.

```python
"""Search reached over HTTP."""

from __future__ import annotations

from chassis.upstream import HopClient
from gateway.domain.models import SearchAnswer
from resilience.deadline import Deadline


class HttpSearchUpstream:
    """One attempt at ``POST /search``; the retry policy lives in the use case."""

    def __init__(self, client: HopClient) -> None:
        self._client = client

    async def search(self, query: str, timeout: float, deadline: Deadline | None, attempt: int) -> SearchAnswer:
        """The answer to the query."""
        answer = await self._client.call(
            "/search", {"query": query}, timeout=timeout, deadline=deadline, attempt=attempt
        )
        return SearchAnswer(
            query, answer["sku"], answer["price"], answer["inStock"], bool(answer["degraded"]), bool(answer["stale"])
        )
```

The adapter that calls search through the `HopClient` and turns the JSON answer into a `SearchAnswer` (`bool(answer["degraded"])` converts the value to a real boolean).

### Search

```python
"""The catalog lookup and the search result."""

from __future__ import annotations

import zlib
from dataclasses import dataclass

CATALOG_SIZE = 20


def resolve_sku(query: str) -> str:
    """Map a query to one of the 20 catalog skus (``sku-001`` to ``sku-020``), the same way every time."""
    normalized = " ".join(query.lower().split())
    return f"sku-{zlib.crc32(normalized.encode()) % CATALOG_SIZE + 1:03d}"


@dataclass(frozen=True, slots=True)
class SearchResult:
    """What a search returns. ``degraded`` means pricing could not be asked in time, so there is no price."""

    query: str
    sku: str
    price: str | None
    in_stock: bool | None
    degraded: bool
    stale: bool
```

`resolve_sku(query)` maps any query to one of 20 skus the same way every time: `" ".join(query.lower().split())` lowercases and collapses spaces; `zlib.crc32(normalized.encode()) % CATALOG_SIZE + 1` hashes the text to a number (CRC32, a checksum) and takes its remainder after dividing by 20, plus 1; `f"sku-{...:03d}"` pads to three digits (`:03d`). `SearchResult` is the value returned.

```python
"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol

from resilience.deadline import Deadline
from search.domain.models import SearchResult


class PricingUpstream(Protocol):
    """One attempt at asking pricing. Raises ``AttemptFailed``."""

    async def quote(
        self, query: str, sku: str, timeout: float, deadline: Deadline | None, attempt: int
    ) -> SearchResult:
        """The priced result for the sku."""


class Telemetry(Protocol):
    """Counters the experiments read."""

    def shed(self) -> None:
        """A request was refused because the concurrency limit was full."""

    def degraded(self) -> None:
        """A partial result was returned because pricing could not be asked in time."""
```

`PricingUpstream.quote(...)` and `Telemetry` (`shed`, `degraded`).

```python
"""Search: resolve the query, then ask pricing."""

from __future__ import annotations

from collections.abc import Callable

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded, Overloaded
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import Profile
from resilience.retry import AttemptFailed, RetriesExhausted, run_naive
from search.application.ports import PricingUpstream, Telemetry
from search.domain.models import SearchResult, resolve_sku

HOP_TIMEOUT = 1.2
MARGIN = 0.05
MIN_USEFUL = 0.1


class PricingUnavailable(Exception):
    """The naive chain's failure: pricing could not be reached after every attempt."""


class SearchProducts:
    """What search does about pricing depends on the profile.

    - ``naive``: three immediate attempts with a 2 second timeout each; if all fail, the error goes up.
    - ``deadline-retry``: one attempt inside what is left of the caller's deadline; on failure, a partial result.
    - ``full``: the same, plus load shedding: beyond ``limiter.limit`` requests in progress, a request is refused at
      once with ``Overloaded`` (a 503 with ``Retry-After``) instead of joining a queue it cannot leave in time.
    """

    def __init__(
        self,
        pricing: PricingUpstream,
        limiter: ConcurrencyLimiter,
        profile: Callable[[], Profile],
        clock: Callable[[], float],
        telemetry: Telemetry,
    ) -> None:
        self._pricing = pricing
        self._limiter = limiter
        self._profile = profile
        self._clock = clock
        self._telemetry = telemetry

    async def __call__(self, query: str, deadline: Deadline | None) -> SearchResult:
        """Return the priced result; raise ``Overloaded``, ``DeadlineExceeded`` or ``PricingUnavailable``."""
        profile = self._profile()
        sku = resolve_sku(query)
        shedding = profile is Profile.FULL
        if shedding and not self._limiter.try_acquire():
            self._telemetry.shed()
            raise Overloaded(retry_after=1.0)
        try:
            if profile is Profile.NAIVE:
                return await self._naive(query, sku)
            return await self._once(query, sku, deadline)
        finally:
            if shedding:
                self._limiter.release()

```

Constants `HOP_TIMEOUT = 1.2`, `MARGIN = 0.05`, `MIN_USEFUL = 0.1`. `PricingUnavailable` is the naive chain's failure. The docstring lists the three profiles. `__call__`: resolve the sku; `shedding = profile is Profile.FULL`; if shedding and `not self._limiter.try_acquire()` record a shed and raise `Overloaded(retry_after=1.0)`; then `try:` run the naive or the single-attempt path and `finally:` release the slot only if one was taken. The `finally` guarantees the slot is returned whatever happens.

```python
    async def _naive(self, query: str, sku: str) -> SearchResult:
        counter = {"n": 0}

        async def attempt(timeout: float) -> SearchResult:
            counter["n"] += 1
            return await self._pricing.quote(query, sku, timeout, None, counter["n"])

        try:
            return await run_naive(attempt)
        except RetriesExhausted as error:
            raise PricingUnavailable(str(error)) from error

    async def _once(self, query: str, sku: str, deadline: Deadline | None) -> SearchResult:
        timeout = HOP_TIMEOUT if deadline is None else deadline.attempt_timeout(self._clock(), HOP_TIMEOUT, MARGIN)
        if timeout < MIN_USEFUL:
            raise DeadlineExceeded
        try:
            return await self._pricing.quote(query, sku, timeout, deadline, 1)
        except AttemptFailed:
            self._telemetry.degraded()
            return SearchResult(query, sku, None, None, degraded=True, stale=False)
```

`_naive` retries immediately with `run_naive` and turns exhaustion into `PricingUnavailable`. `_once` computes the timeout from the deadline (`HOP_TIMEOUT` if there is none), raises `DeadlineExceeded` if too little time is left, makes one attempt, and on `AttemptFailed` returns a degraded `SearchResult` with no price.

```python
"""Pricing reached over HTTP."""

from __future__ import annotations

from chassis.upstream import HopClient
from resilience.deadline import Deadline
from search.domain.models import SearchResult


class HttpPricingUpstream:
    """One attempt at ``POST /price``; the retry policy lives in the use case."""

    def __init__(self, client: HopClient) -> None:
        self._client = client

    async def quote(
        self, query: str, sku: str, timeout: float, deadline: Deadline | None, attempt: int
    ) -> SearchResult:
        """The priced result for the sku."""
        answer = await self._client.call("/price", {"sku": sku}, timeout=timeout, deadline=deadline, attempt=attempt)
        return SearchResult(
            query, sku, answer["price"], answer["inStock"], bool(answer["degraded"]), bool(answer["stale"])
        )
```

The HTTP adapter to pricing.

```python
"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything search needs to start."""

    pricing_url: str
    port: int
    log_level: str
    concurrency_limit: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            pricing_url=reader.text("PRICING_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
            concurrency_limit=reader.integer("SEARCH_CONCURRENCY_LIMIT", 40, minimum=1, maximum=10_000),
        )
        reader.finish()
        return settings
```

Settings: `PRICING_URL` (required), `PORT`, `LOG_LEVEL`, `SEARCH_CONCURRENCY_LIMIT` (default 40, between 1 and 10 000; `10_000` is the number 10000 with an underscore for readability).

```python
"""Prometheus counters for the search service."""

from __future__ import annotations

from chassis.metrics import ServiceMetrics


class PrometheusTelemetry:
    """Implements the ``Telemetry`` port."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._shed = metrics.counter("shed_total", "Requests refused by the concurrency limit")
        self._degraded = metrics.counter("degraded_total", "Partial results returned")

    def shed(self) -> None:
        """A request was refused because the concurrency limit was full."""
        self._shed.inc()

    def degraded(self) -> None:
        """A partial result was returned."""
        self._degraded.inc()
```

Two counters, `shed_total` and `degraded_total`.

```python
"""Search's HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from chassis.admin import admin_router
from chassis.deadline_ctx import current_deadline
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from resilience.errors import DeadlineExceeded, Overloaded
from resilience.profile import ProfileHolder
from search.application.search_products import PricingUnavailable, SearchProducts


class SearchBody(BaseModel):
    """``POST /search``."""

    query: str = Field(min_length=1, max_length=200)


def create_app(
    search: SearchProducts, holder: ProfileHolder, health: HealthRegistry, metrics: ServiceMetrics, store: SpanStore
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="search", docs_url=None, redoc_url=None)
    app.add_middleware(ObservabilityMiddleware, service="search", metrics=metrics, store=store)
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(holder, store))

    @app.post("/search", response_model=None)
    async def run_search(body: SearchBody) -> dict[str, Any] | JSONResponse:
        try:
            result = await search(body.query, current_deadline())
        except Overloaded as error:
            return JSONResponse(
                {"detail": "overloaded"}, status_code=503, headers={"Retry-After": str(int(error.retry_after))}
            )
        except DeadlineExceeded as error:
            raise HTTPException(status_code=504, detail="deadline exceeded") from error
        except PricingUnavailable as error:
            raise HTTPException(status_code=502, detail="pricing unavailable") from error
        return {
            "query": result.query,
            "sku": result.sku,
            "price": result.price,
            "inStock": result.in_stock,
            "degraded": result.degraded,
            "stale": result.stale,
        }

    return app
```

`POST /search` maps the use case's errors to HTTP: `Overloaded` to 503 with `Retry-After`, `DeadlineExceeded` to 504, `PricingUnavailable` to 502 (bad gateway), and returns the result as JSON with camel-case names.

```python
"""The composition root: build adapters, wire the use case, return the application."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from chassis.upstream import HopClient
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import ProfileHolder
from search.application.search_products import SearchProducts
from search.infrastructure.http_pricing import HttpPricingUpstream
from search.infrastructure.settings import Settings
from search.infrastructure.telemetry import PrometheusTelemetry
from search.interfaces.api import create_app


def build_app(settings: Settings) -> FastAPI:
    """Wire the use case and close the HTTP client when the app shuts down."""
    metrics = ServiceMetrics("search")
    store = SpanStore()
    holder = ProfileHolder()
    client = HopClient("search", "pricing", settings.pricing_url, metrics=metrics, store=store)
    use_case = SearchProducts(
        HttpPricingUpstream(client),
        ConcurrencyLimiter(settings.concurrency_limit),
        holder.get,
        time.monotonic,
        PrometheusTelemetry(metrics),
    )
    app = create_app(use_case, holder, HealthRegistry(), metrics, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.close()

    app.router.lifespan_context = lifespan
    return app
```

Wiring: a `HopClient` to pricing, `ConcurrencyLimiter(settings.concurrency_limit)` and the use case; the lifespan closes the client.

```python
"""Process entry point: ``search-serve``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from chassis.errors import ConfigError
from chassis.logging import configure_logging
from search.infrastructure.settings import Settings
from search.interfaces.bootstrap import build_app


async def _serve(settings: Settings) -> None:
    app = build_app(settings)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_config=None, timeout_graceful_shutdown=10, backlog=4096
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    """Read the environment, build the application and serve until SIGTERM, then drain in-flight requests."""
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        configure_logging("search")
        logging.getLogger("search").error(str(error))
        sys.exit(2)
    configure_logging("search", settings.log_level)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
```

The entry point `search-serve` (same structure as the gateway's).

### Pricing

```python
"""Price rules and the quote value object."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from decimal import Decimal

LOW_STOCK = 10
SURGE = Decimal("1.15")


def base_price(sku: str) -> Decimal:
    """A stable base price for a sku (5.00 to 44.99), derived from the sku so no price table is needed."""
    return (Decimal(5) + Decimal(zlib.crc32(sku.encode()) % 4000) / 100).quantize(Decimal("0.01"))


def price_for(sku: str, quantity: int) -> Decimal:
    """The base price, 15 percent higher when fewer than ten units are left."""
    price = base_price(sku)
    return (price * SURGE).quantize(Decimal("0.01")) if quantity < LOW_STOCK else price


@dataclass(frozen=True, slots=True)
class Quote:
    """A price for a sku. ``degraded`` means the stock store was not consulted for this answer;
    ``stale`` means the price comes from an earlier answer or from the base price alone."""

    sku: str
    price: Decimal
    in_stock: bool | None
    degraded: bool = False
    stale: bool = False


def quote_from_stock(sku: str, quantity: int) -> Quote:
    """A fresh quote from a stock reading."""
    return Quote(sku, price_for(sku, quantity), quantity > 0)


def fallback_quote(sku: str, last_known: Quote | None) -> Quote:
    """The degraded answer when the stock store cannot be asked: the last known quote, or the base price."""
    if last_known is not None:
        return Quote(sku, last_known.price, last_known.in_stock, degraded=True, stale=True)
    return Quote(sku, base_price(sku), None, degraded=True, stale=True)
```

The price rules. `base_price(sku)` derives a stable price from the sku with CRC32: `Decimal(5) + Decimal(crc % 4000) / 100` gives 5.00 to 44.99 and `.quantize(Decimal("0.01"))` rounds to two places. `price_for` adds 15 percent (`SURGE = Decimal("1.15")`) when fewer than `LOW_STOCK = 10` units remain. `Quote` has `degraded` and `stale` flags with defaults `False`. `quote_from_stock` builds a fresh quote; `fallback_quote` builds the degraded one from the last known quote or the base price alone.

```python
"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol

from pricing.domain.models import Quote
from resilience.breaker import BreakerState
from resilience.deadline import Deadline


class StockUpstream(Protocol):
    """One attempt at asking the stock store. Raises ``AttemptFailed``."""

    async def check(self, sku: str, timeout: float, deadline: Deadline | None, attempt: int) -> int:
        """Units available for the sku."""


class QuoteCache(Protocol):
    """The last good quote per sku, the fallback while the stock store is unreachable."""

    def get(self, sku: str) -> Quote | None:
        """The last known quote, or ``None``."""

    def put(self, quote: Quote) -> None:
        """Remember a fresh quote."""


class Telemetry(Protocol):
    """Counters the experiments read."""

    def fallback(self, reason: str) -> None:
        """A degraded answer was returned."""

    def breaker_rejected(self) -> None:
        """The breaker refused to call the stock store."""

    def breaker_state(self, state: BreakerState) -> None:
        """The breaker's state, after it changed."""
```

`StockUpstream.check`, `QuoteCache` (`get`, `put`) and `Telemetry` (`fallback`, `breaker_rejected`, `breaker_state`).

```python
"""Quote a price: ask the stock store, protected by the profile's policy."""

from __future__ import annotations

from collections.abc import Callable

from pricing.application.ports import QuoteCache, StockUpstream, Telemetry
from pricing.domain.models import Quote, fallback_quote, quote_from_stock
from resilience.breaker import CircuitBreaker
from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from resilience.profile import Profile
from resilience.retry import AttemptFailed, RetriesExhausted, run_naive

HOP_TIMEOUT = 0.7
MARGIN = 0.05
MIN_USEFUL = 0.1


class StockUnavailable(Exception):
    """The naive chain's failure: the stock store could not be reached after every attempt."""


class QuotePrice:
    """What pricing does about the stock store depends on the profile.

    - ``naive``: three immediate attempts with a 2 second timeout each; if all fail, the error goes up.
    - ``deadline-retry``: one attempt inside what is left of the caller's deadline; on failure, a degraded answer.
    - ``full``: the same, behind a circuit breaker. While the breaker is open the stock store is not called at all
      and the degraded answer is returned at once.
    """

    def __init__(
        self,
        stock: StockUpstream,
        cache: QuoteCache,
        breaker: CircuitBreaker,
        profile: Callable[[], Profile],
        clock: Callable[[], float],
        telemetry: Telemetry,
    ) -> None:
        self._stock = stock
        self._cache = cache
        self._breaker = breaker
        self._profile = profile
        self._clock = clock
        self._telemetry = telemetry
```

Constants `HOP_TIMEOUT = 0.7`, `MARGIN`, `MIN_USEFUL`. `StockUnavailable` is the naive failure. The constructor receives the stock upstream, the cache, the breaker, the profile function, a clock and telemetry.

```python
    async def __call__(self, sku: str, deadline: Deadline | None) -> Quote:
        """Return a quote; raise :class:`StockUnavailable` (naive) or :class:`DeadlineExceeded`."""
        profile = self._profile()
        if profile is Profile.NAIVE:
            return await self._naive(sku)
        if profile is Profile.FULL and not self._breaker.allow():
            self._telemetry.breaker_rejected()
            return self._degrade(sku, "breaker open")
        timeout = HOP_TIMEOUT if deadline is None else deadline.attempt_timeout(self._clock(), HOP_TIMEOUT, MARGIN)
        if timeout < MIN_USEFUL:
            raise DeadlineExceeded
        try:
            quantity = await self._stock.check(sku, timeout, deadline, 1)
        except AttemptFailed as error:
            if profile is Profile.FULL:
                self._breaker.record_failure()
                self._telemetry.breaker_state(self._breaker.state)
            return self._degrade(sku, error.reason)
        if profile is Profile.FULL:
            self._breaker.record_success()
            self._telemetry.breaker_state(self._breaker.state)
        return self._fresh(sku, quantity)

    async def _naive(self, sku: str) -> Quote:
        counter = {"n": 0}

        async def attempt(timeout: float) -> int:
            counter["n"] += 1
            return await self._stock.check(sku, timeout, None, counter["n"])

        try:
            quantity = await run_naive(attempt)
        except RetriesExhausted as error:
            raise StockUnavailable(str(error)) from error
        return self._fresh(sku, quantity)

    def _fresh(self, sku: str, quantity: int) -> Quote:
        quote = quote_from_stock(sku, quantity)
        self._cache.put(quote)
        return quote

    def _degrade(self, sku: str, reason: str) -> Quote:
        self._telemetry.fallback(reason)
        return fallback_quote(sku, self._cache.get(sku))
```

`__call__`: with the naive profile, `_naive`. With the full profile, first ask the breaker `allow()`; if refused, record it and return the degraded answer at once (`"breaker open"`). Compute the timeout; if too small raise `DeadlineExceeded`. Make one attempt; on failure, with the full profile tell the breaker (`record_failure`) and the telemetry, then return the degraded answer with the failure's reason; on success tell the breaker (`record_success`) and return a fresh quote. `_naive` retries immediately and wraps exhaustion in `StockUnavailable`. `_fresh` stores the quote in the cache; `_degrade` records the fallback and returns the last known quote.

```python
"""An in-memory cache of the last good quote per sku."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from pricing.domain.models import Quote


class InMemoryQuoteCache:
    """Remembers up to ``max_entries`` quotes for ``ttl_seconds`` each. A production system would use a shared cache."""

    def __init__(self, clock: Callable[[], float], ttl_seconds: float = 3600.0, max_entries: int = 10_000) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[float, Quote]] = OrderedDict()

    def get(self, sku: str) -> Quote | None:
        """The last known quote, or ``None`` when absent or older than the time to live."""
        entry = self._entries.get(sku)
        if entry is None or self._clock() - entry[0] > self._ttl:
            return None
        return entry[1]

    def put(self, quote: Quote) -> None:
        """Remember a fresh quote, evicting the oldest entry when full."""
        self._entries[quote.sku] = (self._clock(), quote)
        self._entries.move_to_end(quote.sku)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
```

An in-memory cache of the last good quote per sku, with a time to live and a size limit. `OrderedDict` remembers insertion order: `put` stores `(time, quote)`, `move_to_end` marks it newest, and `popitem(last=False)` evicts the oldest. `get` returns `None` for an absent or too-old entry.

```python
"""The stock store reached over HTTP."""

from __future__ import annotations

from chassis.upstream import HopClient
from resilience.deadline import Deadline


class HttpStockUpstream:
    """One attempt at ``POST /stock``; the retry policy lives in the use case."""

    def __init__(self, client: HopClient) -> None:
        self._client = client

    async def check(self, sku: str, timeout: float, deadline: Deadline | None, attempt: int) -> int:
        """Units available for the sku."""
        answer = await self._client.call("/stock", {"sku": sku}, timeout=timeout, deadline=deadline, attempt=attempt)
        return int(answer["quantity"])
```

The adapter to the stock store: it posts `{"sku": sku}` to `/stock` and returns `int(answer["quantity"])`.

```python
"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything pricing needs to start."""

    stock_url: str
    port: int
    log_level: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            stock_url=reader.text("STOCK_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
        )
        reader.finish()
        return settings
```

Settings: `STOCK_URL` (required), `PORT`, `LOG_LEVEL`.

```python
"""Prometheus counters for the pricing service."""

from __future__ import annotations

from chassis.metrics import ServiceMetrics
from resilience.breaker import BreakerState


class PrometheusTelemetry:
    """Implements the ``Telemetry`` port with counters and a gauge in the service's registry."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._fallbacks = metrics.counter("fallbacks_total", "Degraded answers returned", ["reason"])
        self._rejected = metrics.counter("breaker_rejected_total", "Calls the open breaker refused locally")
        self._state = metrics.gauge("breaker_state", "0 closed, 1 open, 2 half-open")

    def fallback(self, reason: str) -> None:
        """A degraded answer was returned."""
        kind = reason if reason in {"breaker open", "timeout"} else "error"
        self._fallbacks.labels(kind).inc()

    def breaker_rejected(self) -> None:
        """The breaker refused to call the stock store."""
        self._rejected.inc()
        self._state.set(int(BreakerState.OPEN))

    def breaker_state(self, state: BreakerState) -> None:
        """The breaker's state, after it changed."""
        self._state.set(int(state))
```

Counters `fallbacks_total` (labeled by a small set of reasons) and `breaker_rejected_total`, and a gauge `breaker_state` (0 closed, 1 open, 2 half-open): `set(int(state))` converts the enumeration to a number.

```python
"""Pricing's HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from chassis.admin import admin_router
from chassis.deadline_ctx import current_deadline
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from pricing.application.quote_price import QuotePrice, StockUnavailable
from resilience.errors import DeadlineExceeded
from resilience.profile import ProfileHolder


class PriceBody(BaseModel):
    """``POST /price``."""

    sku: str


def create_app(
    quote_price: QuotePrice, holder: ProfileHolder, health: HealthRegistry, metrics: ServiceMetrics, store: SpanStore
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="pricing", docs_url=None, redoc_url=None)
    app.add_middleware(ObservabilityMiddleware, service="pricing", metrics=metrics, store=store)
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(holder, store))

    @app.post("/price")
    async def price(body: PriceBody) -> dict[str, Any]:
        try:
            quote = await quote_price(body.sku, current_deadline())
        except DeadlineExceeded as error:
            raise HTTPException(status_code=504, detail="deadline exceeded") from error
        except StockUnavailable as error:
            raise HTTPException(status_code=502, detail="stock unavailable") from error
        return {
            "sku": quote.sku,
            "price": str(quote.price),
            "inStock": quote.in_stock,
            "degraded": quote.degraded,
            "stale": quote.stale,
        }

    return app
```

`POST /price`: `DeadlineExceeded` becomes 504, `StockUnavailable` 502; the price is returned as text (`str(quote.price)`).

```python
"""The composition root: build adapters, wire the use case, return the application."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from chassis.upstream import HopClient
from pricing.application.quote_price import QuotePrice
from pricing.infrastructure.cache import InMemoryQuoteCache
from pricing.infrastructure.http_stock import HttpStockUpstream
from pricing.infrastructure.settings import Settings
from pricing.infrastructure.telemetry import PrometheusTelemetry
from pricing.interfaces.api import create_app
from resilience.breaker import CircuitBreaker
from resilience.profile import ProfileHolder


def build_app(settings: Settings) -> FastAPI:
    """Wire the use case and close the HTTP client when the app shuts down."""
    metrics = ServiceMetrics("pricing")
    store = SpanStore()
    holder = ProfileHolder()
    client = HopClient("pricing", "stock", settings.stock_url, metrics=metrics, store=store)
    breaker = CircuitBreaker(time.monotonic, failure_ratio=0.5, min_calls=10, window_seconds=10.0, open_seconds=5.0)
    use_case = QuotePrice(
        HttpStockUpstream(client),
        InMemoryQuoteCache(time.monotonic),
        breaker,
        holder.get,
        time.monotonic,
        PrometheusTelemetry(metrics),
    )
    app = create_app(use_case, holder, HealthRegistry(), metrics, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.close()

    app.router.lifespan_context = lifespan
    return app
```

Wiring: a `HopClient` to stock, a `CircuitBreaker(time.monotonic, failure_ratio=0.5, min_calls=10, window_seconds=10.0, open_seconds=5.0)`, the cache and the use case.

```python
"""Process entry point: ``pricing-serve``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from chassis.errors import ConfigError
from chassis.logging import configure_logging
from pricing.infrastructure.settings import Settings
from pricing.interfaces.bootstrap import build_app


async def _serve(settings: Settings) -> None:
    app = build_app(settings)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_config=None, timeout_graceful_shutdown=10, backlog=4096
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    """Read the environment, build the application and serve until SIGTERM, then drain in-flight requests."""
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        configure_logging("pricing")
        logging.getLogger("pricing").error(str(error))
        sys.exit(2)
    configure_logging("pricing", settings.log_level)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
```

The entry point `pricing-serve`.

### Stock

```python
"""Value objects and errors of the stock store."""

from __future__ import annotations

from dataclasses import dataclass


class InjectedFailure(Exception):
    """A failure the experiment asked for (the configured fail rate)."""


class UnknownSku(LookupError):
    """No stock row exists for the sku."""

    def __init__(self, sku: str) -> None:
        super().__init__(f"unknown sku {sku!r}")
        self.sku = sku


class InvalidFaults(ValueError):
    """The requested latency or fail rate is out of range."""


@dataclass(frozen=True, slots=True)
class Faults:
    """What the experiment injects at the bottom of the chain: added latency and a share of failing calls."""

    latency_ms: int = 50
    fail_rate: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.latency_ms <= 60_000:
            raise InvalidFaults("latency_ms must be between 0 and 60000")
        if not 0.0 <= self.fail_rate <= 1.0:
            raise InvalidFaults("fail_rate must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class StockAnswer:
    """Whether a sku is in stock, and how many units."""

    sku: str
    quantity: int

    @property
    def in_stock(self) -> bool:
        """True when at least one unit is left."""
        return self.quantity > 0
```

`InjectedFailure` is a failure the experiment asked for. `UnknownSku` (a `LookupError`) keeps the sku. `InvalidFaults` is a range error. `Faults(latency_ms=50, fail_rate=0.0)` validates itself: `not 0 <= self.latency_ms <= 60_000` and `not 0.0 <= self.fail_rate <= 1.0` are chained comparisons. `StockAnswer.in_stock` is a property: `quantity > 0`.

```python
"""Interfaces the use case needs."""

from __future__ import annotations

from typing import Protocol


class StockRepository(Protocol):
    """Reads stock levels from the store's own database."""

    async def quantity(self, sku: str) -> int | None:
        """Units available, or ``None`` for an unknown sku."""
```

`StockRepository.quantity(sku)` is `async` and returns the units or `None`.

```python
"""Answer a stock check, slowly or wrongly when the experiment says so, honoring the caller's deadline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from stock.application.ports import StockRepository
from stock.domain.models import Faults, InjectedFailure, StockAnswer, UnknownSku


class CheckStock:
    """The stock store's one operation.

    Work is limited to ``capacity`` at a time (a store has finite connections and CPU). A request that arrives with a
    deadline gives up as soon as the deadline passes, whether it is still waiting for capacity or already
    "working", so abandoned requests stop costing anything. A request without a deadline (the naive chain) is carried
    through to the end even if its caller left long ago, which is how a backlog outlives the slowdown that built it.
    """

    def __init__(
        self,
        repository: StockRepository,
        faults: Callable[[], Faults],
        capacity: asyncio.Semaphore,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float],
    ) -> None:
        self._repository = repository
        self._faults = faults
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def __call__(self, sku: str, deadline: Deadline | None) -> StockAnswer:
        """Return the stock of ``sku``; raise ``InjectedFailure``, ``UnknownSku`` or ``DeadlineExceeded``."""
        if deadline is None:
            return await self._work(sku)
        remaining = deadline.remaining(self._clock())
        if remaining <= 0:
            raise DeadlineExceeded
        try:
            async with asyncio.timeout(remaining):
                return await self._work(sku)
        except TimeoutError as error:
            raise DeadlineExceeded from error

    async def _work(self, sku: str) -> StockAnswer:
        async with self._capacity:
            faults = self._faults()
            await self._sleep(faults.latency_ms / 1000.0)
            if self._rand() < faults.fail_rate:
                raise InjectedFailure
            quantity = await self._repository.quantity(sku)
        if quantity is None:
            raise UnknownSku(sku)
        return StockAnswer(sku, quantity)
```

The use case, explained under "A capacity limit that also honors a deadline" in `asyncio.md`: a semaphore limits work to the configured capacity; a request with a deadline runs inside `asyncio.timeout(remaining)` and gives up as soon as it passes; one without a deadline is carried to the end. `_work` waits for a slot, reads the current faults, sleeps for the injected latency, randomly raises `InjectedFailure` with probability `fail_rate`, reads the quantity and returns the answer or raises `UnknownSku`.

```python
"""The faults currently injected; replaced as a whole when the experiment changes them."""

from __future__ import annotations

from stock.domain.models import Faults


class FaultState:
    """Holds the current :class:`Faults`."""

    def __init__(self, initial: Faults) -> None:
        self._faults = initial

    def get(self) -> Faults:
        """The faults in force now."""
        return self._faults

    def set(self, faults: Faults) -> None:
        """Replace the faults."""
        self._faults = faults
```

Holds the current `Faults`; `set` replaces them as a whole.

```python
"""PostgreSQL adapter for stock levels."""

from __future__ import annotations

from chassis.postgres import Pool


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

The one query (see `sql.md`, `psycopg.md`).

```python
"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the stock store needs to start."""

    database_url: str
    port: int
    log_level: str
    capacity: int
    latency_ms: int
    fail_rate: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            database_url=reader.text("DATABASE_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
            capacity=reader.integer("STOCK_CONCURRENCY", 16, minimum=1, maximum=1000),
            latency_ms=reader.integer("LATENCY_MS", 50, minimum=0, maximum=60_000),
            fail_rate=reader.number("FAIL_RATE", 0.0, minimum=0.0, maximum=1.0),
        )
        reader.finish()
        return settings
```

Settings: `DATABASE_URL`, `PORT`, `LOG_LEVEL`, `STOCK_CONCURRENCY` (default 16), `LATENCY_MS` (default 50) and `FAIL_RATE` (default 0.0).

```python
"""The stock store's HTTP API: check a sku, and the admin route that injects latency and errors."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

from chassis.admin import admin_router
from chassis.deadline_ctx import current_deadline
from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics, metrics_router
from chassis.observability import ObservabilityMiddleware
from chassis.tracing import SpanStore
from chassis.web import health_router
from resilience.errors import DeadlineExceeded
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults, InjectedFailure, InvalidFaults, UnknownSku
from stock.infrastructure.fault_state import FaultState


class StockBody(BaseModel):
    """``POST /stock``."""

    sku: str


class FaultsBody(BaseModel):
    """``POST /admin/config``: any field left out keeps its value."""

    latency_ms: int | None = None
    fail_rate: float | None = None


def create_app(
    check_stock: CheckStock,
    fault_state: FaultState,
    health: HealthRegistry,
    metrics: ServiceMetrics,
    store: SpanStore,
) -> FastAPI:
    """Build the application around an already-wired use case."""
    app = FastAPI(title="stock", docs_url=None, redoc_url=None)
    app.add_middleware(ObservabilityMiddleware, service="stock", metrics=metrics, store=store)
    app.include_router(health_router(health))
    app.include_router(metrics_router(metrics))
    app.include_router(admin_router(None, store))

    @app.post("/stock")
    async def stock(body: StockBody) -> dict[str, Any]:
        try:
            answer = await check_stock(body.sku, current_deadline())
        except InjectedFailure as error:
            raise HTTPException(status_code=500, detail="simulated failure") from error
        except UnknownSku as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except DeadlineExceeded as error:
            raise HTTPException(status_code=504, detail="deadline exceeded") from error
        return {"sku": answer.sku, "inStock": answer.in_stock, "quantity": answer.quantity}
```

`POST /stock` maps `InjectedFailure` to 500, `UnknownSku` to 404 and `DeadlineExceeded` to 504; `StockBody` and `FaultsBody` are the request models (see `pydantic.md`).

```python
    admin = APIRouter(prefix="/admin", tags=["admin"])

    @admin.get("/config")
    async def read_config() -> dict[str, Any]:
        faults = fault_state.get()
        return {"latencyMs": faults.latency_ms, "failRate": faults.fail_rate}

    @admin.post("/config")
    async def set_config(body: FaultsBody) -> dict[str, Any]:
        current = fault_state.get()
        try:
            fault_state.set(
                Faults(
                    current.latency_ms if body.latency_ms is None else body.latency_ms,
                    current.fail_rate if body.fail_rate is None else body.fail_rate,
                )
            )
        except InvalidFaults as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return await read_config()

    app.include_router(admin)
    return app
```

The admin routes `GET` and `POST /admin/config`: `read_config` returns the current faults; `set_config` builds new `Faults` from the body, keeping a current value when the field is `None` (`current.latency_ms if body.latency_ms is None else body.latency_ms`), and answers 422 for `InvalidFaults`.

```python
"""The composition root: build adapters, wire the use case, return the application."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.postgres import create_pool
from chassis.tracing import SpanStore
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults
from stock.infrastructure.fault_state import FaultState
from stock.infrastructure.postgres import PostgresStockRepository
from stock.infrastructure.settings import Settings
from stock.interfaces.api import create_app


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
    use_case = CheckStock(
        PostgresStockRepository(pool),
        faults.get,
        asyncio.Semaphore(settings.capacity),
        clock=time.monotonic,
        rand=random.random,
    )
    app = create_app(use_case, faults, health, metrics, SpanStore())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        checker = asyncio.create_task(refresh_database_check())
        yield
        checker.cancel()
        await pool.close()

    app.router.lifespan_context = lifespan
    return app
```

`async def build_app` opens the pool with `await`, creates the fault state, metrics and health registry, and a dictionary `state` with the last database check. `refresh_database_check` is a background loop that every two seconds tests the database and stores `ok` or `failed`; `database_ready` raises if the stored value is not `ok`. The use case gets a semaphore of the configured capacity (`asyncio.Semaphore(settings.capacity)`). The lifespan starts the loop with `asyncio.create_task`, and after `yield` cancels it and closes the pool.

```python
"""Process entry point: ``stock-serve``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from chassis.errors import ConfigError
from chassis.logging import configure_logging
from stock.infrastructure.settings import Settings
from stock.interfaces.bootstrap import build_app


async def _serve(settings: Settings) -> None:
    app = await build_app(settings)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_config=None, timeout_graceful_shutdown=10, backlog=4096
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    """Read the environment, build the application and serve until SIGTERM, then drain in-flight requests."""
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        configure_logging("stock")
        logging.getLogger("stock").error(str(error))
        sys.exit(2)
    configure_logging("stock", settings.log_level)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
```

The entry point `stock-serve`: `build_app` is `async`, so `_serve` awaits it.

## Every Python file, one by one: the scripts

### `scripts/experiment.py`

```python
"""Run the lesson's experiments: python scripts/experiment.py <name>. Needs the stack (make up)."""

import asyncio
import sys

from tests.system import experiments as e

NAMES = {
    "amplification-naive": lambda: e.amplification("naive"),
    "slowdown-naive": lambda: e.slowdown("naive"),
    "amplification-policy": lambda: e.amplification("deadline-retry"),
    "slowdown-policy": lambda: e.slowdown("deadline-retry"),
    "outage": e.outage,
    "shedding": e.shedding,
    "slowdown-full": lambda: e.slowdown("full"),
}


async def main(names: list[str]) -> None:
    for name in names:
        print(f"\n===== {name}")
        await NAMES[name]()


if __name__ == "__main__":
    chosen = sys.argv[1:] or ["--help"]
    if chosen == ["all"]:
        chosen = list(NAMES)
    if any(n not in NAMES for n in chosen):
        sys.exit(f"usage: experiment.py {{{'|'.join(NAMES)}|all}}")
    asyncio.run(main(chosen))
```

`NAMES` maps an experiment name to a function; `lambda: e.amplification("naive")` is a function that calls the experiment with an argument. `main(names)` is `async`: for each name it prints a heading and `await`s the experiment. In the `__main__` block: `sys.argv[1:] or ["--help"]` takes the names from the command line; `all` runs every experiment; an unknown name exits with a usage message; `asyncio.run(main(chosen))` starts the event loop.

### `scripts/trace.py`

```python
"""Send one request through the chain with its trace forced on and print the span tree."""

import asyncio

from tests.system import harness


async def main() -> None:
    await harness.wait_ready()
    await harness.set_profile("naive")
    await harness.set_faults(latency_ms=50, fail_rate=0.0)
    trace_id, summary = await harness.forced_request()
    print(f"gateway answered {summary['status']} in {summary['seconds']} s; trace {trace_id}")
    print(harness.render_trace(await harness.fetch_trace(trace_id)))


if __name__ == "__main__":
    asyncio.run(main())
```

Sends one request through the chain with its trace forced on and prints the span tree: wait for the services, set the profile to `naive` and the injected faults to 50 ms and 0, send a forced request, print the status and time, fetch the trace and print it as a tree (`harness.render_trace`).

The tests are Python files too. They are walked through, line by line, in `pytest.md`.

## Try it yourself

1. Check the version:

   ```bash
   uv run python --version
   ```

   Expect `Python 3.12.` and a patch number.

2. Watch the backoff grow and be capped (a `rand` of 1.0 gives the maximum of each range):

   ```bash
   uv run python -c "
   from resilience.backoff import Backoff
   b = Backoff()
   print([round(b.delay(n, 1.0), 3) for n in range(1, 7)])"
   ```

   Expect `[0.05, 0.1, 0.2, 0.4, 0.5, 0.5]`.

3. See a deadline shrink:

   ```bash
   uv run python -c "
   from resilience.deadline import Deadline
   d = Deadline.after(0.0, 2.0)
   print(d.remaining(0.5), d.attempt_timeout(0.5, 1.8, 0.05), d.header_value(0.5))"
   ```

   Expect `1.5 1.45 1500`: 1.5 seconds left; the hop may use the smaller of its own limit (1.8) and what is left minus the margin (1.5 − 0.05 = 1.45); the header sent to the next hop says 1500 milliseconds.

4. Try to change a frozen object:

   ```bash
   uv run python -c "from resilience.deadline import Deadline; Deadline(1.0).expires_at = 2.0"
   ```

   Expect `dataclasses.FrozenInstanceError: cannot assign to field 'expires_at'`.

## Mistakes people make

- **A mutable default argument.** Writing `def f(items=[])` shares one list between all calls. Use `field(default_factory=dict)` in a dataclass, or `None` and create the list inside the function.
- **Catching `Exception` and moving on.** It hides bugs. Catch broad `Exception` only where you log the error and either re-raise it or report it.
- **Comparing floats for money.** This code uses `Decimal`.
- **Thinking type hints are checked at run time.** They are not. `mypy` checks them (see `mypy.md`).
- **Importing a module for its side effects.** Importing a file runs it. Entry points here sit under `if __name__ == "__main__":` and `main()` so that tests can import them safely.

## Check yourself

1. What does `frozen=True` do on a dataclass?
   Answer: it makes instances read-only after they are created; assigning to a field raises `FrozenInstanceError`.
2. Why would a class take a clock or a `sleep` function as a parameter, with the real one as the default?
   Answer: tests can pass fakes, so they run instantly and deterministically while production uses the real `time.sleep` or clock.
3. What is the difference between a module and a package?
   Answer: a module is one `.py` file; a package is a folder of modules (with `__init__.py`).
4. What does `raise NewError(...) from error` add over a plain `raise`?
   Answer: it keeps the original error as the cause, so the trace shows both.
5. When does the code under `if __name__ == "__main__":` run?
   Answer: only when the file is run as a program, not when another file imports it.

## Further reading

- Python 3.12 documentation: https://docs.python.org/3.12/
- What's new in Python 3.12 (the `[T]` generic syntax): https://docs.python.org/3.12/whatsnew/3.12.html
- `dataclasses`: https://docs.python.org/3.12/library/dataclasses.html
- `contextvars`: https://docs.python.org/3.12/library/contextvars.html
- `typing.Protocol`: https://docs.python.org/3.12/library/typing.html#typing.Protocol
