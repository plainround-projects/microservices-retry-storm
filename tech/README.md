# Technology notes

These notes teach every technology this repository uses, from zero, through the repository's own files. You do not need to know any of them before you start. Each note says what the technology is, why the lesson needs it, where it appears, how it works underneath, and then walks through this repository's own code line by line. Each ends with commands to try and five questions to check yourself.

The lesson itself is in the main [README](../README.md). These notes explain the tools it is built from.

## Ten minutes? Read these three

1. [asyncio.md](asyncio.md): how one process can hold thousands of waiting requests, and what a timeout really cancels. The storm lives here.
2. [python.md](python.md): the language features behind the policy objects (deadline, backoff, retry budget, breaker).
3. [prometheus-client.md](prometheus-client.md): the counters the lesson reads amplification from.

## Reading order

Start with the language, then the build and file tools, then the runtime pieces, then the tests and checks.

| File | Its role in this repository |
| --- | --- |
| [python.md](python.md) | Python 3.12: the language of the policy library, the chassis, the four services, the experiments |
| [asyncio.md](asyncio.md) | asyncio: `async`, `await`, timeouts, semaphores, tasks |
| [bash.md](bash.md) | Bash and `sh`: SQL application and the shell inside Makefile recipes |
| [make.md](make.md) | GNU Make: the named commands (`make up`, `make run`, `make experiment-naive`, `make test`) |
| [uv.md](uv.md) | uv: installs the locked packages and runs every Python tool |
| [toml.md](toml.md) | TOML: the `pyproject.toml` files |
| [yaml.md](yaml.md) | YAML: the format of the compose file and the CI workflow |
| [json.md](json.md) | JSON: request bodies, log lines, spans, and the committed experiment results |
| [git.md](git.md) | Git: the history, `.gitignore`, `.editorconfig` |
| [docker.md](docker.md) | Docker: containers, images, hardening, and stopping services during experiments |
| [dockerfile.md](dockerfile.md) | Dockerfile syntax: the four-stage build of every service image |
| [docker-compose.md](docker-compose.md) | Docker Compose: the database and the four-service chain as one system |
| [postgresql.md](postgresql.md) | PostgreSQL: the stock store's database and its limits |
| [sql.md](sql.md) | SQL: the schema, the seed and the checks |
| [psycopg.md](psycopg.md) | psycopg 3 and its async pool: how Python talks to PostgreSQL without blocking |
| [fastapi.md](fastapi.md) | FastAPI and Starlette: the HTTP APIs, the middleware, the admin and health routes |
| [pydantic.md](pydantic.md) | pydantic: the request models |
| [uvicorn.md](uvicorn.md) | uvicorn: the web server, the listen backlog and graceful shutdown |
| [httpx.md](httpx.md) | httpx: the one-attempt hop client, the in-memory transport, the load generator |
| [prometheus-client.md](prometheus-client.md) | prometheus_client: the counters, gauge and histogram exposed at `/metrics` |
| [pytest.md](pytest.md) | pytest and pytest-cov: unit, in-process chain, integration and end-to-end tests |
| [pytest-asyncio.md](pytest-asyncio.md) | pytest-asyncio: running `async def` tests |
| [ruff.md](ruff.md) | ruff: linting and formatting |
| [mypy.md](mypy.md) | mypy: static type checking in strict mode |
| [import-linter.md](import-linter.md) | import-linter: the layering rules enforced as a check |
| [pip-audit.md](pip-audit.md) | pip-audit: known-vulnerability check of the locked packages |
| [github-actions.md](github-actions.md) | GitHub Actions: the CI workflow |

## Conventions in these notes

- A command in a code block is meant to be run from the repository root unless it starts with `cd`.
- Commands marked as needing the stack expect `make up` to have finished.
- Line quotes are copied from the files as they are in this repository.
