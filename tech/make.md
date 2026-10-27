# GNU Make

## What it is

`make` is a command-line tool that runs named recipes from a file called `Makefile`. A **recipe** is a list of shell commands. You type `make test` and make finds the rule called `test` and runs its commands. Make was invented to rebuild only what changed. This repository uses it for a simpler reason: one short, memorable name for every long command.

## Why this repository uses it

The lesson has six measurements and several checks, each with a long command. The README tells the learner to type `make up`, `make run`, `make experiment-naive`, `make test`, `make lint`. Make gives each a short name, and CI uses the same names.

## Where it appears

| Path | What it does with make |
| --- | --- |
| `Makefile` | Every named command of the repository |
| `README.md`, `.github/workflows/ci.yml` | Call the targets (`make up`, `make check-package PKG=...`, `make experiment-naive`) |

## How it works underneath

A Makefile is made of **rules**:

```text
target: prerequisite1 prerequisite2
<TAB>command
<TAB>command
```

- The **target** is the name you type after `make`.
- The **prerequisites** are other targets that must run first. Make runs them in order, and runs each at most once per invocation.
- Each **command** line must start with a real TAB character, not spaces. Make hands each line to a shell. Each line runs in its own shell, so `cd` on one line does not carry to the next unless the lines are joined with `&&` or `;`.
- A line starting with `@` is not echoed before it runs. A line starting with `-` ignores a failure.
- A target that is not a file is **phony**. `.PHONY: test` tells make not to look for a file named `test`. Without it, a file called `test` would make `make test` say "nothing to do".

**Variables.** `NAME := value` sets a variable once. `$(NAME)` uses it. Inside a recipe, `$$` writes a literal `$` for the shell (so `$$p` is the shell variable `$p`). `$(wildcard services/*)` expands to the matching file names. `$(notdir a/b)` gives `b`. Variables that are `export`ed also reach the commands as environment variables.

**Order of work.** Make reads the whole file first, then runs the requested target's prerequisites left to right, then its commands. If any command exits with a non-zero code, make stops and prints `Error`.

macOS ships GNU Make 3.81, an old version. Everything used here works on it.

## Walkthrough of this repository's use

### The header

```make
# make up | run | test | lint | down   (make help lists every target)
SHELL := /bin/bash
COMPOSE := docker compose --ansi never
PACKAGES := $(wildcard libs/*) $(wildcard services/*)
SERVICES := $(notdir $(wildcard services/*))
STOCK_PSQL := $(COMPOSE) exec -T stock-db psql -U stock_owner -d stock -v ON_ERROR_STOP=1
PY := PYTHONPATH=. uv run python

-include .env
export
export UV_FROZEN := 1

.PHONY: help env install up db queries design run demo experiment-naive experiment-storm experiment-policy experiment-outage \
        experiment-shedding experiment-full smoke trace lint format test test-integration test-system audit check \
        check-package logs down clean
```

- `SHELL := /bin/bash` makes recipes run under bash on every machine.
- `COMPOSE := docker compose --ansi never` stores a command; `--ansi never` turns off colors so logs stay readable.
- `PACKAGES := $(wildcard libs/*) $(wildcard services/*)` is a list of folders (both libraries and all four services); `$(wildcard ...)` expands a pattern. `SERVICES := $(notdir $(wildcard services/*))` strips the folder part: `gateway pricing search stock`.
- `STOCK_PSQL := ...` is the long command for running `psql` in the stock database container: `exec -T` runs in a running container without a terminal, `-U` the user, `-d` the database, `-v ON_ERROR_STOP=1` stops at the first SQL error.
- `PY := PYTHONPATH=. uv run python` runs scripts from the repository root; `PYTHONPATH=.` lets `scripts/experiment.py` import `tests.system`.
- `-include .env` reads `.env` if it exists (`-` ignores a missing file), and `export` passes every variable to the commands. `export UV_FROZEN := 1` is explained in `uv.md`.
- `.PHONY` lists targets that are names, not files; the trailing `\` continues the line.

### Help and setup

```make
help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | sed -E 's/:.*## /\t/' | sort

env:
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }

install: ## create .venv from uv.lock
	uv sync --all-packages

up: env install ## build the images and start the stock database and the four services
	$(COMPOSE) up -d --build --wait --wait-timeout 1200

db: ## apply the stock store's sql/ again (schema, seed)
	$(COMPOSE) run --rm stock-db-init

queries: ## run the stock store's sql/003_queries.sql
	$(STOCK_PSQL) < services/stock/sql/003_queries.sql
```

`help` selects every line that contains `##` and shows `target<TAB>text`. `up: env install` has two prerequisites, then runs `docker compose up -d --build --wait --wait-timeout 1200`. `db` re-runs the init job; `queries` redirects a SQL file into `psql` with `<`.

### The experiments

```make
design: ## lesson step 1: the policy page against the numbers in the code
	uv run pytest tests/system/test_policy_design.py -q

smoke: ## one request through the chain, with its trace printed
	$(PY) scripts/trace.py

# The lesson's measurements. Each one restarts the four services first, so every counter starts at zero. They take
# a few minutes each and print the numbers the README quotes; the raw results are written to docs/results/.
experiment-naive: ## step 2: calls per layer with naive retries and 30 percent failures at the stock store
	$(PY) scripts/experiment.py amplification-naive

experiment-storm: ## step 3: the stock store slows to 1.5 s under load, then recovers: p99, errors, in-flight, recovery
	$(PY) scripts/experiment.py slowdown-naive

experiment-policy: ## step 4: deadlines, one retry layer with a budget: the same two measurements
	$(PY) scripts/experiment.py amplification-policy slowdown-policy

experiment-outage: ## step 5: the stock store stopped entirely under the full policy
	$(PY) scripts/experiment.py outage

experiment-shedding: ## step 5: search sheds the excess with 503 and Retry-After
	$(PY) scripts/experiment.py shedding

experiment-full: ## step 6: the step 3 experiment again with the full policy
	$(PY) scripts/experiment.py slowdown-full

run: smoke experiment-naive ## the quick path: one traced request and the amplification measurement

demo: experiment-naive experiment-storm experiment-policy experiment-outage experiment-shedding experiment-full ## every experiment, in lesson order
```

Each `experiment-*` target is one lesson measurement. `$(PY) scripts/experiment.py amplification-naive` runs a Python function by name. The comment above explains: each experiment restarts the four services first so every counter starts at zero, and writes its raw result to `docs/results/`. `experiment-policy` passes two names, so it runs two measurements. `run` and `demo` have only prerequisites: `run` is `smoke` then `experiment-naive`; `demo` runs every experiment in lesson order. `design` runs one pytest file that checks the policy page against the numbers in the code.

### Quality targets

```make
lint: install ## ruff, formatting, mypy and import rules for every package
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	@for p in $(PACKAGES); do (cd $$p && echo "== mypy $$p" && uv run mypy) || exit 1; done
	@for s in $(SERVICES); do (cd services/$$s && echo "== import rules $$s" && uv run lint-imports) || exit 1; done

format: install ## apply ruff's formatting and safe fixes
	uv run ruff check --fix .
	uv run ruff format .

# Unit tests need no services. The domain and application of every service, and all of resilience, stay at 95% or more.
test: install ## unit tests with coverage for every package, and the design page against the code (no services needed)
	@for p in $(PACKAGES); do \
	  name=$$(basename $$p); \
	  (cd $$p && echo "== unit tests $$p" && uv run pytest tests/unit -m "not integration" --cov=$$name --cov-report=term-missing:skip-covered -q \
	    && if [ -d src/$$name/domain ]; then uv run coverage report --include="src/$$name/domain/*,src/$$name/application/*" --fail-under=95; fi \
	    && if [ $$name = resilience ]; then uv run coverage report --fail-under=95; fi) || exit 1; \
	done
	uv run pytest tests/system -q
```

- `lint` runs ruff, then `uv run mypy` for the root `tests` and `scripts`, then mypy in every package folder, then the layering checker in every service.
- `format` fixes (`ruff check --fix .`, `ruff format .`).
- `test` loops over packages and for each runs the unit tests with coverage. The two `&&` clauses add the coverage gates: 95 percent for `domain` and `application` of any package that has a `domain/`, and 95 percent of the whole package for `resilience`, the library of pure policy objects. Then `uv run pytest tests/system -q` runs the in-process chain tests once.

### Stack tests, audit, check

```make
test-integration: install ## the stock store's adapter against its database (make up first)
	cd services/stock && uv run pytest tests/integration -m integration -q

test-system: install ## the chain across the running containers: every profile answers, shed and degraded paths (make up first)
	uv run pytest tests/e2e -m e2e -q

audit: install ## check the locked dependencies for known vulnerabilities
	uv export --frozen --all-packages --no-emit-workspace --no-hashes -q > /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt
	uv run pip-audit --no-deps --disable-pip -r /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt

check: lint test ## everything that needs no services

check-package: ## CI: format, lint, types, import rules and unit tests for one package (PKG=services/search)
	uv run ruff check $(PKG)
	uv run ruff format --check $(PKG)
	cd $(PKG) && uv run mypy
	@if [ -d $(PKG)/src/$(notdir $(PKG))/domain ]; then cd $(PKG) && uv run lint-imports; fi
	cd $(PKG) && uv run pytest tests/unit -m "not integration" -q
```

`test-integration` and `test-system` need `make up` first. `audit` exports the lock file and audits it (see `pip-audit.md`). `check` has only prerequisites (`lint`, `test`). `check-package` is the CI form for one package, chosen with `PKG=...` on the command line.

### Stop

```make
logs: ## follow the service logs
	$(COMPOSE) logs -f --no-log-prefix gateway search pricing stock

down: ## stop everything and delete the database
	$(COMPOSE) down -v --remove-orphans

clean: down
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .import_linter_cache .coverage
```

`down` runs `docker compose down -v --remove-orphans`: stop and remove containers, the network and (`-v`) the volume, here the stock database. `clean` runs `down` first, then deletes local caches.

## Try it yourself

1. List the targets:

   ```bash
   make help
   ```

   Expect one line per documented target: a name, a tab and a sentence.

2. See what a target would do without running it (`-n`):

   ```bash
   make -n up
   ```

   Expect the `.env` test, `uv sync --all-packages` and the `docker compose --ansi never up -d --build --wait --wait-timeout 1200` line.

3. See an experiment command:

   ```bash
   make -n experiment-policy
   ```

   Expect `PYTHONPATH=. uv run python scripts/experiment.py amplification-policy slowdown-policy`.

4. Run a cheap target that needs no services:

   ```bash
   make design
   ```

   Expect pytest to report that the policy page agrees with the code (`passed`).

## Mistakes people make

- **Spaces instead of a TAB** before a command: the error is `missing separator`.
- **Expecting `cd` to persist** between recipe lines.
- **Forgetting `.PHONY`** for a target with no file of that name.
- **Writing `$p` for a shell variable** inside a recipe. Make eats it; write `$$p`.
- **Running `make` with no target** and being surprised: it runs the first target in the file.
- **Hiding errors with `-`** or `|| true` and then wondering why a failed step did not stop the run.

## Check yourself

1. What does a prerequisite do?
   Answer: it is another target that make runs first.
2. Why must a recipe line start with a TAB?
   Answer: that is how make tells commands from other lines; spaces give `missing separator`.
3. Why do two recipe lines `cd x` and `ls` not list `x`?
   Answer: each line runs in its own shell, so the `cd` is lost; join them with `&&`.
4. What does `.PHONY` do?
   Answer: it marks targets that are not files, so make always runs them.
5. How do you write the shell variable `$p` inside a recipe?
   Answer: `$$p`.

## Further reading

- GNU Make manual: https://www.gnu.org/software/make/manual/make.html
- Phony targets: https://www.gnu.org/software/make/manual/make.html#Phony-Targets
