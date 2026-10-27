# Python 3.12

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
