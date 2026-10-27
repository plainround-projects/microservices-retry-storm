# uv

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

`uv` is a command-line tool for Python projects. It creates a **virtual environment** (a private folder, here `.venv/`, that holds the Python packages one project needs, so projects do not disturb each other). It installs packages into that folder and records the exact versions in a **lock file**. It also runs commands inside the environment.

A **package** is a bundle of Python code someone else wrote, such as `fastapi`. A **dependency** is a package your code needs. A **lock file** (`uv.lock`) lists every package, including the packages your packages need, each pinned to one exact version. Anyone who installs from the lock file gets the same versions.

## Why this repository uses it

Six Python packages (`resilience`, `chassis` and four services) must be installed together and tested against the same versions of everything. A uv workspace gives one lock file and one environment for all six. The Dockerfile and the CI file use the same lock file, so a laptop, a container and CI run the same packages.

## Where it appears

| Path | What it does with uv |
| --- | --- |
| `pyproject.toml` (root) | Declares the workspace members and the development tools |
| `libs/*/pyproject.toml`, `services/*/pyproject.toml` | Each package's own needs; packages name their siblings as workspace sources |
| `uv.lock` | The pinned versions of every package |
| `.python-version` | `3.12` |
| `Makefile` | Sets `UV_FROZEN`, and runs `uv sync`, `uv run`, `uv export` |
| `Dockerfile` | Copies the `uv` program in and runs `uv sync` and `uv pip install` |
| `.github/workflows/ci.yml` | Installs uv 0.11.2 with `astral-sh/setup-uv` and runs `uv sync --frozen` |

## How it works underneath

1. You describe your needs in `pyproject.toml` (see `toml.md`): "I need `fastapi` version 0.115 or newer".
2. `uv lock` (or any command that needs a lock) asks the Python package index, PyPI, which versions fit all the needs at once, and writes the answer to `uv.lock`. This is a text file; open it and read it.
3. `uv sync` reads `uv.lock` and makes `.venv/` match it exactly: it downloads or reuses packages from uv's cache and links them in.
4. `uv run <command>` makes sure the environment is in sync, then runs the command with `.venv/bin` first on the `PATH`, so `python`, `pytest` and the other tools come from the project's environment.

A **workspace** is one repository with several Python packages that share one lock file and one `.venv/`. Each package keeps its own `pyproject.toml`. A package can depend on a sibling by name, and uv installs the sibling from the folder, not from PyPI.

A **frozen** command (`--frozen`, or the environment variable `UV_FROZEN=1`) never changes the lock file. If `pyproject.toml` and `uv.lock` disagree, a non-frozen command re-locks; a frozen one just uses what the lock says.

## Walkthrough of this repository's use

### The workspace root

```toml
[dependency-groups]
dev = [
    "pytest>=8,<10",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5",
    "ruff>=0.6",
    "mypy>=1.11",
    "import-linter>=2.0",
    "pip-audit>=2.7",
    "httpx>=0.27",
    "psycopg[binary]>=3.2,<4",
    "prometheus-client>=0.20",
]

[tool.uv]
package = false
default-groups = ["dev"]

[tool.uv.workspace]
members = ["libs/*", "services/*"]
```

- `[dependency-groups] dev = [...]` lists tools only developers need. `"pytest>=8,<10"` means "8 or 9". `"psycopg[binary]>=3.2,<4"` means "3.2 or newer, below 4, with the optional extra `binary`". `pytest-asyncio` lets pytest run `async def` tests (see `pytest-asyncio.md`) and `prometheus-client` is needed by the tests that read metrics.
- `[tool.uv]` with `package = false` says the root is not itself a package; `default-groups = ["dev"]` installs the `dev` group by default.
- `[tool.uv.workspace] members = ["libs/*", "services/*"]` lists the packages: every folder under `libs/` and `services/`.

### Members that use their siblings

```toml
dependencies = ["chassis", "resilience", "fastapi>=0.115", "uvicorn>=0.30"]

[project.scripts]
stock-serve = "stock.interfaces.main:main"

[tool.uv.sources]
chassis = { workspace = true }
resilience = { workspace = true }
```

`dependencies = ["chassis", "resilience", ...]` would normally be looked up on PyPI. `[tool.uv.sources]` with `{ workspace = true }` says "these are workspace members": uv installs the local `libs/chassis` and `libs/resilience`. The `resilience` library itself has `dependencies = []`: it needs nothing outside the standard library.

### The Makefile's use

```make
-include .env
export
export UV_FROZEN := 1
```

`export UV_FROZEN := 1` puts `UV_FROZEN=1` in the environment of every command make starts, so every `uv run` and `uv sync` uses the lock file as written.

```make
install: ## create .venv from uv.lock
	uv sync --all-packages
```

`uv sync --all-packages` makes `.venv/` match the lock for all six packages. `lint`, `test`, `audit` and the other targets depend on `install`, so a fresh clone works without a separate setup step.

```make
audit: install ## check the locked dependencies for known vulnerabilities
	uv export --frozen --all-packages --no-emit-workspace --no-hashes -q > /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt
	uv run pip-audit --no-deps --disable-pip -r /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt
```

`uv export` prints the lock file as `name==version` lines: `--frozen` obeys the lock, `--all-packages` includes every member, `--no-emit-workspace` leaves out our own packages (not on PyPI), `--no-hashes` leaves out file hashes, `-q` is quiet. `pip-audit` reads the result (see `pip-audit.md`).

### In the Dockerfile

```dockerfile
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=ghcr.io/astral-sh/uv@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY --from=manifests /m/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace --all-packages
```

`UV_LINK_MODE=copy` copies files instead of linking to uv's cache. `UV_PYTHON_DOWNLOADS=never` stops uv downloading a Python (the base image has 3.12). `UV_PROJECT_ENVIRONMENT=/app/.venv` fixes the environment path for the final image. `uv sync --frozen --no-dev --no-install-workspace --all-packages` installs only third-party runtime dependencies for all packages, skipping dev tools and our own packages (the next stage adds those).

```dockerfile
RUN uv pip install --no-cache --python /app/.venv/bin/python --no-deps ./libs/* "./services/${SERVICE}"
```

`uv pip install --no-cache --python /app/.venv/bin/python --no-deps ./libs/* "./services/${SERVICE}"` installs our own packages into that environment without touching dependencies.

## Try it yourself

1. See the tool version:

   ```bash
   uv --version
   ```

   Expect `uv 0.11.2` or close.

2. See the workspace as a tree, one level deep:

   ```bash
   UV_FROZEN=1 uv tree --package stock --depth 1
   ```

   Expect `stock v0.1.0` with `chassis`, `resilience`, `fastapi` and `uvicorn` below it.

3. See the pinned Prometheus client in the exported list:

   ```bash
   UV_FROZEN=1 uv export --frozen --all-packages --no-emit-workspace --no-hashes -q | grep -E "^(prometheus-client|pytest-asyncio|psycopg)=="
   ```

   Expect `prometheus-client==0.26.0`, `psycopg==3.3.6` and `pytest-asyncio==1.4.1`.

4. Run a tool through the environment:

   ```bash
   UV_FROZEN=1 uv run ruff --version
   ```

   Expect `ruff 0.17.3`.

## Mistakes people make

- **Editing `uv.lock` by hand.** It is generated. Change `pyproject.toml` and run `uv lock`.
- **Forgetting to commit `uv.lock`.** Without it two machines can get different versions.
- **Using `pip install` inside a uv project.** It changes `.venv/` behind uv's back. Use `uv add` or edit `pyproject.toml`.
- **Running tools with the system Python.** Typing `pytest` may find another copy. `uv run pytest` always uses the project's copy.
- **Expecting `uv run` in a workspace root to install every member.** It installs what the root asks for. Use `uv sync --all-packages` to install all members.

## Check yourself

1. What is the difference between `pyproject.toml` and `uv.lock`?
   Answer: `pyproject.toml` says what you need (version ranges); `uv.lock` says exactly which versions were chosen, including indirect dependencies.
2. What does `uv run pytest` do that plain `pytest` does not?
   Answer: it first syncs the environment, then runs the copy of pytest that lives in the project's `.venv/`.
3. What does `--frozen` protect against?
   Answer: against uv quietly re-locking and changing versions; the lock file is used as it is.
4. What is a workspace?
   Answer: several Python packages in one repository that share one lock file and one environment.
5. Where do the installed packages physically live?
   Answer: in the `.venv/` folder (copies or links taken from uv's cache).

## Further reading

- uv documentation (this repository's CI pins uv 0.11.2): https://docs.astral.sh/uv/
- Projects and `pyproject.toml`: https://docs.astral.sh/uv/concepts/projects/
- Workspaces: https://docs.astral.sh/uv/concepts/projects/workspaces/
- Locking and syncing: https://docs.astral.sh/uv/concepts/projects/sync/
