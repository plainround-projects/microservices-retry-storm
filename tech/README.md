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

## Words you will meet in every note

Each note explains its own words where they first appear. These are the everyday words behind all of them. Read this list once; come back to it whenever a word feels unfamiliar.

- **Terminal, shell, command.** A *terminal* is a window where you type text commands. The *shell* is the program inside it that reads what you type and starts other programs. A *command* is one line you type, such as `make test`. Words after the program name are *arguments*; arguments that start with `-` or `--` are *flags* (options), for example `--version`.
- **Folder, path.** A *folder* (directory) holds files. A *path* is the address of a file or folder, such as `services/stock/sql/001_schema.sql`. A path starting at the repository's top folder is called *relative*; one starting at `/` is *absolute*.
- **Program, process.** A *program* is code stored in a file. A *process* is a program while it runs. Each process has a number, its *process id*, and when it ends it leaves an *exit code*: 0 means success, any other number means failure. Shells and CI systems use the exit code to decide whether a step worked.
- **Standard output, standard error, signal.** A process writes normal output to *standard output* (stdout) and diagnostics to *standard error* (stderr); a terminal shows both. A *signal* is a short message the operating system sends to a process. `SIGTERM` means "please stop" and `SIGKILL` means "stop at once, no clean-up".
- **Thread.** A *thread* is a separate line of execution inside one process. Two threads can work at the same time and share the process's memory.
- **Network, host, port, localhost.** Computers talk over a *network*. A *host* is a computer's name or address. A *port* is a number (0 to 65535) that tells the receiving computer which program on it should get the data. `localhost` and `127.0.0.1` both mean "this same computer". A *server* is a program that waits for connections on a port; a *client* is a program that connects.
- **HTTP, request, response, status code, header, body, API, URL.** *HTTP* is the language web programs speak. A *request* has a method (`GET` reads, `POST` sends data), a *URL* (the address, for example `http://localhost:8080/health/live`), *headers* (named extra facts such as `Content-Type`) and sometimes a *body* (the data). The *response* has a *status code* (200 means OK, 404 not found, 422 the request was refused as invalid, 500 and above the server failed, 503 unavailable) and its own headers and body. An *API* is a set of such requests that a program offers to other programs. Traffic in this repository is plain HTTP on your own machine; *TLS* (the encryption behind `https://`, which needs a *certificate* that proves who the server is) is not used here.
- **JSON.** *JSON* is a text format for data: an *object* is `{"name": value}` with text keys, an *array* is `[1, 2]`, and values are text in double quotes, numbers, `true`, `false` or `null`. See `json.md`.
- **Environment variable.** A named text value that the operating system hands to each process when it starts, such as `PORT=8080`. Programs use them for settings that differ between machines. A file named `.env` lists them for Docker Compose.
- **Package, library, dependency, version, pin, lock file.** A *package* is a bundle of code someone else wrote. A *library* is a package your code calls. A *dependency* is a package your code needs. A *version* is a number like `0.162.0`. To *pin* is to require one exact version. A *lock file* records the exact version of every package, so every machine installs the same ones.
- **Virtual environment.** A private folder (here `.venv/`) holding the packages one project needs, so projects on the same machine do not disturb each other.
- **Container, image, volume.** An *image* is a read-only package of files and settings. A *container* is a running copy of an image, kept apart from the rest of the machine. A *volume* is storage that outlives a container. See `docker.md`.
- **Database, table, row, column, SQL, transaction.** A *database* stores data in *tables*; a table has *columns* (named fields) and *rows* (records). *SQL* is the language used to ask a database for things. A *transaction* is a group of changes that either all happen or none do. See `sql.md` and `postgresql.md`.
- **Role, permission, least privilege.** A database *role* is a login with *permissions* (what it may read or change). *Least privilege* means giving a program only the permissions it needs.
- **Timeout, retry, backoff, jitter.** A *timeout* is a limit on how long to wait. A *retry* is trying again after a failure. *Backoff* is waiting longer before each retry; *jitter* is adding a random part to the wait so many clients do not retry at the same moment.
- **Test, fake, assertion.** A *test* is a small program that checks that code behaves as claimed. A *fake* is a simple stand-in for a real part. An *assertion* (`assert`) is a statement that fails the test if its condition is false.
- **Log, metric, trace.** A *log* is a line a program writes about what happened. A *metric* is a number a program keeps (how many requests it has served). A *trace* is the record of one request's path through several programs.
