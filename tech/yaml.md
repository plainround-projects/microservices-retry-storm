# YAML

## What it is

YAML is a file format for structured settings, like TOML and JSON (see `toml.md`, `json.md`). It uses indentation instead of brackets, so it reads almost like an outline. Docker Compose and GitHub Actions both use it.

## Why this repository uses it

Docker Compose (the whole chain in one file) and GitHub Actions (CI) take YAML. Neither lets you choose another format.

## Where it appears

| Path | What it describes |
| --- | --- |
| `docker-compose.yml` | The stock database, its init job and the four services (see `docker-compose.md`) |
| `.github/workflows/ci.yml` | The CI jobs and their steps (see `github-actions.md`) |

## How it works underneath

A YAML file describes three kinds of things.

- A **mapping** is a set of `key: value` pairs. Nesting is shown by indenting with spaces (never tabs): everything indented under a key belongs to it.
- A **sequence** is a list. Block style puts one `- item` per line. Flow style puts it on one line: `[a, b, c]`. A mapping can also be written on one line: `{ key: value }`.
- A **scalar** is a single value: text, a number, `true`, `false`, `null`. Quotes are optional for most text. Quote text when it could be mistaken for something else, for example `"no"` (an old YAML reads bare `no` as false), `"3.10"` (a bare `3.10` is the number 3.1), or text containing `: ` or `#`.
- `#` starts a comment.
- An **anchor** (`&name`) marks a block so it can be reused; an **alias** (`*name`) copies it; the **merge key** `<<: *name` merges the anchored mapping into the current one. Keys written next to the merge override the merged ones.
- A program reads the whole file into nested dictionaries and lists, then looks for the keys it knows.

## Walkthrough of this repository's use

### An anchor and a merge key

```yaml
x-service: &service
  restart: on-failure:10
  read_only: true
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  stop_grace_period: 20s
  tmpfs: [/tmp]
```
```yaml
  stock:
    <<: *service
    build: { context: ., args: { SERVICE: stock } }
    image: storm-stock:dev
```

`x-service: &service` is a top-level key starting with `x-`, which Compose ignores; `&service` names the mapping. Under `stock:` the line `<<: *service` merges that mapping in, so all four services share their hardening settings and add only their own keys.

### Block and flow styles side by side

```yaml
  stock-db:
    image: postgres:18
    environment:
      POSTGRES_USER: stock_owner
      POSTGRES_PASSWORD: ${STOCK_OWNER_PASSWORD:?run make env}
      POSTGRES_DB: stock
    ports: ["127.0.0.1:${STOCK_DB_PORT:-43432}:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U stock_owner -d stock"]
      interval: 2s
      timeout: 3s
      retries: 40
```

`ports: ["127.0.0.1:${STOCK_DB_PORT:-43432}:5432"]` is a sequence on one line (flow style). `healthcheck:` is a nested mapping in block style. `test: ["CMD-SHELL", "pg_isready ..."]` is a flow sequence. `${VAR:-default}` is Compose's own syntax inside a plain text value, not YAML.

```yaml
    volumes:
      - ./scripts/apply-sql.sh:/scripts/apply-sql.sh:ro
      - ./services/stock/sql:/sql:ro
    depends_on:
      stock-db: { condition: service_healthy }
    restart: "no"
```

`volumes:` is a block sequence: each `-` is one item. `depends_on: stock-db: { condition: service_healthy }` is a mapping with one key written in flow style. `restart: "no"` is quoted on purpose: bare `no` is read as the boolean false by YAML 1.1 parsers.

### The CI file

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

A mapping with `name`, `on`, `jobs`. `on: [push, pull_request]` is a flow sequence. `matrix: package: [libs/resilience, libs/chassis, ...]` is a mapping holding a flow sequence. `steps:` is a block sequence of mappings: each `-` starts one step with `uses` or `run`. `${{ matrix.package }}` is GitHub's expression syntax, not YAML.

## Try it yourself

1. Read the workflow as data (`--with pyyaml` adds the PyYAML library for this one run; it needs network access the first time):

   ```bash
   uv run --frozen --with pyyaml python -c "import yaml; d = yaml.safe_load(open('.github/workflows/ci.yml')); print(list(d['jobs']), d[True])"
   ```

   Expect `['package', 'root', 'audit', 'system'] ['push', 'pull_request']`. The key `on` came back as the boolean `True`: YAML 1.1 reads bare `on` as true, the gotcha from the list above.

2. See the anchor expanded:

   ```bash
   uv run --frozen --with pyyaml python -c "import yaml; d = yaml.safe_load(open('docker-compose.yml')); s = d['services']; print(s['gateway']['stop_grace_period'], s['search']['read_only'], s['stock']['cap_drop'])"
   ```

   Expect `20s True ['ALL']`.

## Mistakes people make

- **Tabs for indentation.** Not allowed.
- **Wrong indentation by one space.** The key silently belongs to another parent, or the file fails to parse.
- **Unquoted values that change meaning:** `no`, `on`, `3.10`, a leading `0`.
- **A colon in text** such as `command: echo a: b` (quote it).
- **Assuming a misspelled key will be reported.** Many tools ignore unknown keys, but Docker Compose reports unknown top-level ones.

## Check yourself

1. How does YAML show that a key is inside another?
   Answer: by indenting it with spaces under the parent.
2. How do you write a list on one line?
   Answer: `[a, b, c]`.
3. What do `&name`, `*name` and `<<: *name` do?
   Answer: define a reusable block, copy it, and merge it into a mapping.
4. Why quote `"3.10"`?
   Answer: unquoted it is read as the number 3.1.
5. Can you indent with tabs?
   Answer: no, only spaces.

## Further reading

- YAML 1.2.2 specification: https://yaml.org/spec/1.2.2/
- Learn YAML in Y minutes (informal overview): https://learnxinyminutes.com/yaml/
