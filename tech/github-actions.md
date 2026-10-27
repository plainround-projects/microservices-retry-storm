# GitHub Actions

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

GitHub Actions is GitHub's built-in automation service. A **workflow** is a YAML file (see `yaml.md`) in `.github/workflows/`. GitHub reads it and, when a chosen event happens (a push, a pull request), starts a fresh virtual machine, called a **runner**, and runs the steps you listed. It is the repository's CI: continuous integration, meaning "check every change automatically".

## Why this repository uses it

The lesson's mechanisms are code, and code can regress. The workflow runs the cheap checks for every package, the checks of the experiment tooling, a dependency audit, and, on a machine with Docker, the stack with the quick experiment, so a change that breaks the measured behavior shows up on the commit.

## Where it appears

| Path | What it does |
| --- | --- |
| `.github/workflows/ci.yml` | The only workflow, with four jobs |

## How it works underneath

- `on:` lists the events that start the workflow. `on: [push, pull_request]` means every push and every pull request.
- A workflow has **jobs**. Jobs run in parallel on separate runners unless you order them. A job has `runs-on` (the type of machine, such as `ubuntu-latest`) and a list of **steps**.
- A step is either `uses: owner/name@version` (run a ready-made **action**, a reusable program) or `run: command` (run a shell command). `with:` passes inputs to an action.
- `actions/checkout@v4` copies the repository's files onto the fresh runner. Without it the runner has an empty folder.
- `strategy.matrix` runs the same job once per value in a list. `${{ matrix.package }}` reads the current value. `fail-fast: false` lets the other copies finish when one fails.
- `${{ ... }}` is GitHub's expression syntax. `if: failure()` runs a step only if an earlier step failed; `if: always()` runs it regardless (used for clean-up).
- A step fails if its command exits non-zero, which fails the job and shows a red mark on the commit.
- The runner is thrown away afterwards, so nothing carries over between runs except what you cache or upload.

## Walkthrough of this repository's use

### Job 1: one run per package

```yaml
name: ci

on: [push, pull_request]

jobs:
  package:
    name: Format, lint, types, import rules and unit tests (${{ matrix.package }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        package: [libs/resilience, libs/chassis, services/gateway, services/search, services/pricing, services/stock]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.11.2"
      - run: uv sync --frozen --all-packages --python 3.12
      - run: make check-package PKG=${{ matrix.package }}
```

- `on: [push, pull_request]` triggers it; `fail-fast: false` lets all six matrix runs finish even if one fails.
- `matrix.package` lists the two libraries and the four services; `${{ matrix.package }}` reads the current value, also in the job's display name.
- `actions/checkout@v4` gets the code. `astral-sh/setup-uv@v6` with `version: "0.11.2"` installs that exact uv. `uv sync --frozen --all-packages --python 3.12` installs the locked packages.
- `make check-package PKG=...` is the same target a developer can run (see `make.md`): format check, lint, types, import rules, unit tests.

### Job 2: the experiment tooling

```yaml
  root:
    name: Format, lint and types of the experiments
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.11.2"
      - run: uv sync --frozen --all-packages --python 3.12
      - run: uv run ruff check . && uv run ruff format --check . && uv run mypy
      - run: uv run pytest tests/system -q
```

`uv run ruff check . && uv run ruff format --check . && uv run mypy` checks the root, and `uv run pytest tests/system -q` runs the in-process chain and the policy-page check: the full mechanisms of the lesson, with no Docker.

### Job 3: the audit

```yaml
  audit:
    name: Dependency audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.11.2"
      - run: uv sync --frozen --all-packages --python 3.12
      - run: cp .env.example .env && make audit
```

`cp .env.example .env && make audit`: the audit target uses `COMPOSE_PROJECT_NAME` from `.env` for its temporary file name (see `pip-audit.md`).

### Job 4: the stack

```yaml
  system:
    name: Integration and system tests against docker compose, and the quick experiment
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.11.2"
      - run: make up
      - run: make test-integration test-system
      - run: make experiment-naive
      - if: failure()
        run: docker compose logs --no-color
      - if: always()
        run: make down
```

- `timeout-minutes: 30` stops a hung job.
- `make up` builds and starts the system (Docker is preinstalled on `ubuntu-latest`).
- `make test-integration test-system` runs two targets in one command: the stock adapter against its database, and the chain across the containers.
- `make experiment-naive` runs the quick measurement (amplification under naive retries), which is a real run of the experiment on the CI machine.
- `if: failure()` prints all container logs only when something failed. `if: always()` then runs `make down` whatever happened.

## Try it yourself

1. Read the workflow as data:

   ```bash
   uv run --frozen --with pyyaml python -c "import yaml; d = yaml.safe_load(open('.github/workflows/ci.yml')); [print(k, '->', v['name']) for k, v in d['jobs'].items()]"
   ```

   Expect four lines: `package`, `root`, `audit`, `system`, each with its display name.

2. Run the command CI runs for one package:

   ```bash
   make check-package PKG=libs/resilience
   ```

   Expect ruff, ruff format, mypy and pytest to run in turn and the command to exit 0.

3. Run the local equivalent of jobs 1 to 3:

   ```bash
   make check && make audit
   ```

   Expect unit tests passing for every package, `Contracts: 2 kept, 0 broken.` per service, the `tests/system` run (`19 passed`), and `No known vulnerabilities found`.

## Mistakes people make

- **A CI that differs from the local commands.** Call the same `make` targets so the two cannot drift apart.
- **Floating action versions** (`@main`). Pin a version.
- **Forgetting `actions/checkout`.**
- **Secrets in the workflow file.** Use GitHub's encrypted secrets; this repository needs none.
- **Not cleaning up** services started in a job; use `if: always()`.

## Check yourself

1. What starts a workflow?
   Answer: an event listed under `on:`, such as a push.
2. What is the difference between `uses` and `run`?
   Answer: `uses` runs a ready-made action; `run` runs a shell command.
3. What does a matrix do?
   Answer: runs the same job once for each listed value.
4. Why does the workflow call `make` targets?
   Answer: so CI and developers run exactly the same commands.
5. When does a step with `if: always()` run?
   Answer: always, even after an earlier failure.

## Further reading

- GitHub Actions documentation: https://docs.github.com/en/actions
- Workflow syntax: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- `actions/checkout`: https://github.com/actions/checkout
- `astral-sh/setup-uv`: https://github.com/astral-sh/setup-uv
