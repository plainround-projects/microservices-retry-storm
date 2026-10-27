# mypy

## What it is

mypy is a **static type checker** for Python. "Static" means it reads your code without running it. It uses the type hints you wrote (see `python.md`) and reports places where the types do not fit: passing text where a number is expected, calling a method that does not exist, forgetting that a value can be `None`.

## Why this repository uses it

The policy code works through small interfaces: a clock is a function, a sleep is an async function, an upstream is a `Protocol`. mypy checks that every adapter and every fake keeps matching those shapes, and that code which can receive `None` (a missing deadline, an unknown SKU) handles it.

## Where it appears

| Path | What it does with mypy |
| --- | --- |
| `libs/*/pyproject.toml`, `services/*/pyproject.toml` | A `[tool.mypy]` section per package |
| `pyproject.toml` (root) | A `[tool.mypy]` section for `tests/` and `scripts/` |
| `libs/*/src/*/py.typed`, `services/*/src/*/py.typed` | Markers that the packages ship type hints |
| `Makefile` | `make lint` runs `uv run mypy` at the root and in each package |

## How it works underneath

- mypy reads every file you point it at, follows the imports, and builds a picture of the type of every name. It never starts your program.
- For each call and assignment it asks: does this type fit that type? If not it prints `file:line: error: message  [code]`. The code in brackets, such as `[assignment]`, names the kind of problem.
- **Strict mode** (`strict = true`) turns on a bundle of checks. The main ones: every function must have type hints on its arguments and its result (`disallow_untyped_defs`), calls to untyped functions are errors, and returning `Any` from a typed function is an error.
- `X | None` forces you to handle `None`: you cannot call a method on it before you check it.
- A **Protocol** (see `python.md`) is checked by shape: any class with the right methods fits.
- The exit code is 0 for no errors and 1 otherwise.
- mypy is a different tool from Python. Python ignores type hints when the program runs.

## Walkthrough of this repository's use

### Configuration

```toml
[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false
```

`strict = true`, `python_version = "3.12"`, `files = ["src", "tests"]`: the full strict bundle on both folders. The override relaxes three checks for test modules, where annotations would add noise.

```toml
[tool.mypy]
python_version = "3.12"
strict = true
files = ["tests", "scripts"]
explicit_package_bases = true

[[tool.mypy.overrides]]
module = ["prometheus_client", "prometheus_client.*"]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = ["tests.*", "scripts.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false
check_untyped_defs = true
```

The root run checks `tests` and `scripts`. The first override says `prometheus_client` ships no type hints and mypy should accept that (without it, `from prometheus_client import ...` would be an error). The code covers the gap at the boundary with explicit types such as `Counter` in `chassis/metrics.py`. `check_untyped_defs = true` still checks the body of test functions that have no annotations.

### Types that carry meaning

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
```

`async def run_budgeted[T](...)` is generic: the result type `T` is whatever the `attempt` function returns, and mypy checks that the function passed in really returns an awaitable of that type. `Callable[[float], Awaitable[T]]` is "a function taking a float and returning something awaitable". `float | None`, `Callable[[int, str], None] | None` and similar unions force the code to handle the `None` case.

```python
_current: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar("deadline", default=None)


def current_deadline() -> Deadline | None:
    """The caller's deadline, or ``None`` when the caller sent none (the naive chain)."""
    return _current.get()
```

`ContextVar[Deadline | None]` tells mypy the variable holds a deadline or nothing, so every caller of `current_deadline()` must handle `None` (the naive chain sends no deadline).

```python
Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
```

`Pool` is a named type for "an async pool of connections that return dictionary rows". With it mypy knows `await cursor.fetchone()` is a dictionary or `None`.

### Honest exceptions

```python
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
```

`# type: ignore[arg-type]` silences one named error: `httpx.ASGITransport` expects a precise ASGI type and FastAPI's app type does not match it in the stubs. The bracketed code limits the ignore to that one kind of error on that one line.

## Try it yourself

1. Check a package and the root:

   ```bash
   cd services/gateway && uv run mypy
   cd ../.. && uv run mypy
   ```

   Expect `Success: no issues found in 23 source files` and `Success: no issues found in 12 source files`.

2. Make a type error on the command line:

   ```bash
   uv run mypy --strict -c "x: int = 'a'"
   ```

   Expect `Incompatible types in assignment (expression has type "str", variable has type "int")  [assignment]`.

3. See how a missing `None` check is caught:

   ```bash
   printf 'from resilience.deadline import Deadline\n\n\ndef f(d: Deadline | None) -> float:\n    return d.remaining(0.0)\n' > scratch_none.py
   uv run mypy --strict scratch_none.py; rm scratch_none.py
   ```

   Expect `scratch_none.py:5: error: Item "None" of "Deadline | None" has no attribute "remaining"  [union-attr]`.

## Mistakes people make

- **Assuming mypy checks values.** It checks types, not whether a number is positive.
- **Sprinkling `# type: ignore` or `Any`** to make errors disappear. Each one removes a check. Prefer fixing the type, or `cast` in one place with a comment.
- **Running mypy without the right `files`.** It then checks nothing useful. Here each package lists `files = ["src", "tests"]`.
- **Missing `py.typed`.** Without that marker file, a package's hints are ignored by users of the package.
- **Version drift.** A new mypy can find new errors in old code; the lock file pins the version.

## Check yourself

1. Does mypy run your program?
   Answer: no; it only reads the code and its type hints.
2. What does `strict = true` require of a function?
   Answer: type hints on all arguments and on the return value, among other checks.
3. What does `str | None` force you to do?
   Answer: handle the `None` case before using the value as text.
4. What is `py.typed` for?
   Answer: it tells type checkers that a package ships type hints they should use.
5. What does `# type: ignore` do, and why avoid it?
   Answer: it silences an error on that line, which removes the check.

## Further reading

- mypy documentation (this repository pins mypy 2.4): https://mypy.readthedocs.io/en/stable/
- Command line and configuration: https://mypy.readthedocs.io/en/stable/config_file.html
- The strict flag: https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-strict
- Type hints cheat sheet: https://mypy.readthedocs.io/en/stable/cheat_sheet_py3.html
