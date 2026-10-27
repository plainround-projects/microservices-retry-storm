# TOML

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

TOML is a file format for settings. It is plain text that a person can read and edit, and every tool can parse it into names and values. A **file format** is an agreed way to write information in a file so that programs can read it back.

## Why this repository uses it

Every Python package has a `pyproject.toml`. It names the package, lists what it needs, and holds the settings of the tools that check it (ruff, mypy, pytest, import-linter). One format and one file per package keeps all of it in one place.

## Where it appears

| Path | What it holds |
| --- | --- |
| `pyproject.toml` (root) | The workspace, dev tools, ruff, pytest (including `asyncio_mode`) and mypy settings for `tests/` and `scripts/` |
| `libs/resilience/pyproject.toml` | The policy library: no dependencies, mypy, pytest, coverage |
| `libs/chassis/pyproject.toml` | The shared service parts: FastAPI, httpx, Prometheus client, psycopg |
| `services/gateway/`, `search/`, `pricing/`, `stock/` `pyproject.toml` | A service: dependencies, its command, mypy, pytest, coverage, import-linter contracts |

## How it works underneath

A TOML file is a list of key-value pairs, grouped under headings.

- `name = "value"` sets a key. Text goes in double quotes. Numbers and `true`/`false` have no quotes. A list is `[ "a", "b" ]`. A small table inline is `{ key = "value" }`.
- `[heading]` starts a **table**: a group of keys. A dot in a heading makes a table inside a table, so `[tool.ruff.lint]` is "the `lint` table inside the `ruff` table inside the `tool` table".
- `[[heading]]` (double brackets) adds one item to a **list of tables**. Writing it twice makes a list of two items.
- `#` starts a comment that runs to the end of the line.

A tool reads the file once at start-up. Each tool looks only at its own area: `ruff` reads `[tool.ruff...]`, `mypy` reads `[tool.mypy]` and `pytest` reads `[tool.pytest.ini_options]`. `pyproject.toml` is the standard file where Python tools keep their settings.

## Walkthrough of this repository's use

### The package identity and its command

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "stock"
version = "0.1.0"
description = "The stock store: answers whether a sku is in stock from its own PostgreSQL database, with latency and errors injected for the experiments."
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = ["chassis", "resilience", "fastapi>=0.115", "uvicorn>=0.30"]

[project.scripts]
stock-serve = "stock.interfaces.main:main"

[tool.uv.sources]
chassis = { workspace = true }
resilience = { workspace = true }

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
stock = ["py.typed"]
```

- `[build-system]` says how to build an installable package: `setuptools` 75 or newer through its standard entry point. You never run it yourself; `uv` does.
- `[project]` is the standard description. `requires-python = ">=3.12"` refuses older Pythons. `dependencies` lists what must be installed with it; `">=0.115"` means "0.115 or newer".
- `[project.scripts]` makes a command: after installation `stock-serve` runs the function `main` in `stock.interfaces.main`.
- `[tool.uv.sources]` is uv's setting (see `uv.md`). `[tool.setuptools.packages.find] where = ["src"]` says where the code is; `package-data` ships the `py.typed` marker (see `mypy.md`).

### mypy, pytest and coverage

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

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --strict-markers --import-mode=importlib"
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient`"]
asyncio_mode = "auto"
markers = ["integration: needs the compose stack (make up)"]

[tool.coverage.run]
source = ["stock"]
branch = true

[tool.coverage.report]
show_missing = true
```

- `strict = true`, `files = ["src", "tests"]`. `[[tool.mypy.overrides]]` (double brackets: an item of a list) relaxes three checks for the test modules.
- `addopts` are default command-line options: `-ra` summarizes every non-passing test, `--strict-markers` makes an unregistered marker an error, `--import-mode=importlib` avoids import name clashes. `filterwarnings` hides one known harmless warning about Starlette's test client. `asyncio_mode = "auto"` lets `async def` tests run without a marker (see `pytest-asyncio.md`). `markers` registers `integration`.
- `[tool.coverage.run] branch = true` counts both sides of every `if`.

### import-linter

```toml
[tool.importlinter]
root_package = "stock"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = ["stock.interfaces", "stock.infrastructure", "stock.application", "stock.domain"]

[[tool.importlinter.contracts]]
name = "Domain and application know nothing about FastAPI, PostgreSQL, httpx, Prometheus or the chassis"
type = "forbidden"
source_modules = ["stock.domain", "stock.application"]
forbidden_modules = ["fastapi", "psycopg", "psycopg_pool", "httpx", "prometheus_client", "chassis"]
```

Two `[[tool.importlinter.contracts]]` items make two rules; see `import-linter.md`. The second forbids `prometheus_client` too, so metrics code cannot enter the domain or the application.

### The root: workspace, ruff, pytest, mypy

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
extend-exclude = ["tech"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "N", "C4", "RUF", "PT", "D100", "D101", "D103", "D104"]
# Named errors read better without a suffix (DeadlineTooShort, Overloaded).
ignore = ["N818"]

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["D", "E501"]
"tests/**" = ["D", "E501"]
"scripts/**" = ["D", "E501"]

[tool.ruff.lint.isort]
known-first-party = ["chassis", "resilience", "gateway", "search", "pricing", "stock"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers --import-mode=importlib"
pythonpath = ["."]
asyncio_mode = "auto"
markers = [
    "e2e: the chain across the running containers (make up first)",
    "experiment: a measured experiment from the lesson (make up first, minutes)",
]

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

- `[tool.ruff]` and its children set the width (120), `extend-exclude = ["tech"]` (skip the notes folder: ruff 0.17 formats Python code blocks inside Markdown and these notes quote fragments), the rule families, per-file relaxations, and the first-party names (see `ruff.md`).
- The root `[tool.pytest.ini_options]` covers `tests/system` and `tests/e2e`, sets `asyncio_mode = "auto"` and registers `e2e` and `experiment`.
- The root `[tool.mypy]` checks `tests` and `scripts`; the first override says `prometheus_client` has no type hints and mypy should accept that; the second relaxes checks for tests and scripts.

### The root file, from its first line

```toml
# Workspace root: no code of its own. It pins the development tools and tells uv which packages belong together.
[project]
name = "microservices-retry-storm"
version = "0.1.0"
description = "A four-service chain that turns a slow dependency into an outage with naive retries, then stops it with deadlines, a retry budget, a breaker and load shedding."
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = []
```

The comment says what the file is: a workspace root with no code of its own. `[project]` names the repository, gives a version, a description and `readme = "README.md"`. `requires-python = ">=3.12"` refuses older Pythons. `license = { text = "MIT" }` is an inline table (`{ key = value }` on one line). `authors = [{ name = "Sumit Srivastava" }]` is a list with one inline table. `dependencies = []` is an empty list: the tools are in the `dev` group (see `uv.md`).

### The resilience library, every key

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "resilience"
version = "0.1.0"
description = "The resilience policy as plain objects: deadlines, backoff with jitter, a retry budget, a circuit breaker, a concurrency limiter. No I/O; every clock is injected."
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = []

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
resilience = ["py.typed"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --strict-markers --import-mode=importlib"
asyncio_mode = "auto"

[tool.coverage.run]
source = ["resilience"]
branch = true

[tool.coverage.report]
show_missing = true
```

- `[build-system]` and `[project]` follow the pattern of every package. `dependencies = []` is deliberate: the policy library needs nothing outside the standard library.
- `[tool.setuptools.packages.find]` and `[tool.setuptools.package-data]` look in `src/` and ship `py.typed`.
- `[tool.mypy]`: strict checking of `src` and `tests`; the override relaxes test modules. There is no override for `prometheus_client` here because this package does not import it.
- `[tool.pytest.ini_options]` has `asyncio_mode = "auto"` (the retry loops are async; see `pytest-asyncio.md`).
- `[tool.coverage.run]` measures `resilience` with branch coverage; the Makefile requires 95 percent of the whole package.
- It has no `[project.scripts]` (a library) and no import-linter contracts.

### The chassis package, every key

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "chassis"
version = "0.1.0"
description = "Service chassis: settings, JSON logging, W3C trace context and spans, deadline context, metrics, one-attempt upstream calls, health, async PostgreSQL pool."
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = [
    "resilience",
    "fastapi>=0.115",
    "httpx>=0.27",
    "prometheus-client>=0.20",
    "psycopg[binary,pool]>=3.2,<4",
]

[tool.uv.sources]
resilience = { workspace = true }

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
chassis = ["py.typed"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --strict-markers --import-mode=importlib"
asyncio_mode = "auto"
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient`"]

[tool.coverage.run]
source = ["chassis"]
branch = true

[tool.coverage.report]
show_missing = true
```

Its `dependencies` are `resilience` (a sibling, named in `[tool.uv.sources]` with `{ workspace = true }`) and four third-party libraries: `fastapi`, `httpx`, `prometheus-client` and `psycopg[binary,pool]`. Mypy, pytest (with `asyncio_mode = "auto"` and the warning filter) and coverage are as in the other packages. No scripts, no import-linter contracts: it is infrastructure.

### The gateway package, every key

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "gateway"
version = "0.1.0"
description = "The gateway: sets the deadline and is the only layer that retries, with backoff, jitter and a retry budget."
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = ["chassis", "resilience", "fastapi>=0.115", "uvicorn>=0.30"]

[project.scripts]
gateway-serve = "gateway.interfaces.main:main"

[tool.uv.sources]
chassis = { workspace = true }
resilience = { workspace = true }

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
gateway = ["py.typed"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --strict-markers --import-mode=importlib"
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient`"]
asyncio_mode = "auto"
markers = ["integration: needs the compose stack (make up)"]

[tool.coverage.run]
source = ["gateway"]
branch = true

[tool.coverage.report]
show_missing = true

[tool.importlinter]
root_package = "gateway"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = ["gateway.interfaces", "gateway.infrastructure", "gateway.application", "gateway.domain"]

[[tool.importlinter.contracts]]
name = "Domain and application know nothing about FastAPI, PostgreSQL, httpx, Prometheus or the chassis"
type = "forbidden"
source_modules = ["gateway.domain", "gateway.application"]
forbidden_modules = ["fastapi", "psycopg", "psycopg_pool", "httpx", "prometheus_client", "chassis"]
```

- Lines 1 to 12: build system and description; `dependencies` are the two siblings and the web libraries.
- Lines 14 to 15: `[project.scripts] gateway-serve = "gateway.interfaces.main:main"` makes the command the container runs.
- Lines 17 to 19: workspace sources for `chassis` and `resilience`.
- Lines 21 to 25: the `src` layout and the `py.typed` marker.
- Lines 27 to 36: strict mypy, with the relaxed test override.
- Lines 38 to 44: pytest settings, including `asyncio_mode = "auto"` and the one marker `integration`.
- Lines 46 to 51: coverage.
- Lines 53 to 66: import-linter. The layers list names the four layers; the forbidden contract bars `fastapi`, `psycopg`, `psycopg_pool`, `httpx`, `prometheus_client` and `chassis` from `gateway.domain` and `gateway.application` (see `import-linter.md`).

### The search, pricing and stock packages

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "search"
version = "0.1.0"
description = "Search: resolves a query to a sku and asks pricing, with load shedding and a partial-result fallback."
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = ["chassis", "resilience", "fastapi>=0.115", "uvicorn>=0.30"]

[project.scripts]
search-serve = "search.interfaces.main:main"

[tool.uv.sources]
chassis = { workspace = true }
resilience = { workspace = true }

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
search = ["py.typed"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --strict-markers --import-mode=importlib"
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient`"]
asyncio_mode = "auto"
markers = ["integration: needs the compose stack (make up)"]

[tool.coverage.run]
source = ["search"]
branch = true

[tool.coverage.report]
show_missing = true

[tool.importlinter]
root_package = "search"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = ["search.interfaces", "search.infrastructure", "search.application", "search.domain"]

[[tool.importlinter.contracts]]
name = "Domain and application know nothing about FastAPI, PostgreSQL, httpx, Prometheus or the chassis"
type = "forbidden"
source_modules = ["search.domain", "search.application"]
forbidden_modules = ["fastapi", "psycopg", "psycopg_pool", "httpx", "prometheus_client", "chassis"]
```

`search` has exactly the structure of the gateway: its description reads "Search: resolves a query to a sku and asks pricing, with load shedding and a partial-result fallback"; its command is `search-serve`; its import-linter `root_package` is `search`.

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "pricing"
version = "0.1.0"
description = "Pricing: quotes a price from the stock level, with a circuit breaker and a last-known-price fallback."
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = ["chassis", "resilience", "fastapi>=0.115", "uvicorn>=0.30"]

[project.scripts]
pricing-serve = "pricing.interfaces.main:main"

[tool.uv.sources]
chassis = { workspace = true }
resilience = { workspace = true }

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
pricing = ["py.typed"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --strict-markers --import-mode=importlib"
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient`"]
asyncio_mode = "auto"
markers = ["integration: needs the compose stack (make up)"]

[tool.coverage.run]
source = ["pricing"]
branch = true

[tool.coverage.report]
show_missing = true

[tool.importlinter]
root_package = "pricing"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = ["pricing.interfaces", "pricing.infrastructure", "pricing.application", "pricing.domain"]

[[tool.importlinter.contracts]]
name = "Domain and application know nothing about FastAPI, PostgreSQL, httpx, Prometheus or the chassis"
type = "forbidden"
source_modules = ["pricing.domain", "pricing.application"]
forbidden_modules = ["fastapi", "psycopg", "psycopg_pool", "httpx", "prometheus_client", "chassis"]
```

`pricing` likewise: description "Pricing: quotes a price from the stock level, with a circuit breaker and a last-known-price fallback"; command `pricing-serve`; import-linter root `pricing`.

The `stock` package (whose first 66 lines are quoted earlier in this note) differs in its description, its command `stock-serve`, and its `markers` line.

## Try it yourself

1. Read a value with Python's own TOML parser:

   ```bash
   uv run python -c "import tomllib; d = tomllib.load(open('pyproject.toml','rb')); print(d['tool']['uv']['workspace']['members'], d['tool']['pytest']['ini_options']['asyncio_mode'])"
   ```

   Expect `['libs/*', 'services/*'] auto`.

2. List a service's dependencies:

   ```bash
   uv run python -c "import tomllib; print(tomllib.load(open('services/stock/pyproject.toml','rb'))['project']['dependencies'])"
   ```

   Expect `['chassis', 'resilience', 'fastapi>=0.115', 'uvicorn>=0.30']`.

3. See the parser's complaint about a bad file:

   ```bash
   printf 'name = fastapi\n' > /tmp/bad.toml && uv run python -c "import tomllib; tomllib.load(open('/tmp/bad.toml','rb'))"; rm /tmp/bad.toml
   ```

   Expect `tomllib.TOMLDecodeError: Invalid value (at line 1, column 8)`.

## Mistakes people make

- **Forgetting quotes around text.** `name = fastapi` is an error; write `name = "fastapi"`.
- **Using `[x]` when you mean `[[x]]`.** A second `[x]` heading is an error; a list of items needs the double brackets.
- **Putting a key under the wrong heading.** A key belongs to the heading above it, even many lines later.
- **Tabs and trailing commas confusion.** TOML ignores indentation, and a trailing comma inside a list is allowed, but not inside an inline `{ }` table.
- **Assuming a tool will complain about a misspelled section.** Most tools silently ignore a heading they do not know.

## Check yourself

1. What does `[tool.ruff.lint]` mean?
   Answer: the `lint` table, inside `ruff`, inside `tool`.
2. How do you write a list of two strings?
   Answer: `["a", "b"]`.
3. What is the difference between `[x]` and `[[x]]`?
   Answer: `[x]` is one table; `[[x]]` appends one item to a list of tables.
4. How do you write a comment?
   Answer: with `#`.
5. Which tool reads `[tool.mypy]`?
   Answer: `mypy`. Each tool reads only its own area of `pyproject.toml`.

## Further reading

- The TOML 1.0.0 specification: https://toml.io/en/v1.0.0
- Python's `pyproject.toml` guide: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
