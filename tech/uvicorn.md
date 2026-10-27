# uvicorn

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

uvicorn is a web server for Python. A **web server** is the program that listens on a network port, accepts HTTP connections, reads the requests and hands them to your application. It speaks HTTP to the outside and **ASGI** (see `fastapi.md`) to your code.

## Why this repository uses it

The experiments hold thousands of requests at once in each service, and they stop and restart services between runs. uvicorn runs the asynchronous apps on one event loop, and a larger listen backlog lets a burst of connections wait to be accepted instead of being refused.

## Where it appears

| Path | What it does with uvicorn |
| --- | --- |
| `services/*/src/*/interfaces/main.py` | `uvicorn.Config` and `uvicorn.Server(...).serve()` in `gateway-serve`, `search-serve`, `pricing-serve`, `stock-serve` |
| `Dockerfile` | `exec ${SERVICE}-serve` makes it process 1 |
| `docker-compose.yml` | `stop_grace_period: 20s` |

## How it works underneath

- uvicorn opens a listening socket on a **host** and a **port**. Host `127.0.0.1` accepts connections only from the same machine; host `0.0.0.0` accepts connections from any network interface, which is what a program inside a container needs so that Docker can reach it.
- It runs an **event loop** (an `asyncio` loop) that handles many connections at once on one thread.
- A **signal** is a message the operating system sends a process. `SIGTERM` means "please stop"; Docker sends it first when you stop a container. uvicorn reacts by closing the listening socket (no new connections), letting the requests in flight finish, running the application's shutdown code, and exiting. This is a **graceful shutdown**. `timeout_graceful_shutdown` is how many seconds it will wait for in-flight requests before cutting them off.
- uvicorn has its own logging setup. Passing `log_config=None` tells it not to install one, so the application's logging configuration stays in charge.

## Walkthrough of this repository's use

```python
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
```

- `uvicorn.Config(app, host="0.0.0.0", port=settings.port, log_config=None, timeout_graceful_shutdown=10, backlog=4096)` describes the server. `host="0.0.0.0"` listens on all interfaces (needed in a container). `log_config=None` leaves logging alone. `timeout_graceful_shutdown=10` gives requests in flight 10 seconds to finish after `SIGTERM`. `backlog=4096` is the size of the queue of connections the operating system holds for the server while it is busy; under a burst the default is small enough that connections would be refused.
- `await uvicorn.Server(config).serve()` runs the server inside an event loop that this code starts itself with `asyncio.run(_serve(settings))`. That is the difference from the other repositories, which call `uvicorn.run(...)`: here the app is built by an async function (the stock store opens its pool with `await`), so the program starts the loop first.
- `Settings.from_env(os.environ)` reads and validates the environment; an invalid one logs one JSON error and exits with code 2.

The times fit inside each other: Docker waits `stop_grace_period: 20s` after `SIGTERM` before `SIGKILL`; uvicorn waits up to 10 seconds for requests; the lifespan hook then cancels background tasks and closes the pool (see `asyncio.md`).

## Try it yourself

1. See the version:

   ```bash
   uv run uvicorn --version
   ```

   Expect `Running uvicorn 0.54.0 with CPython 3.12.` and the platform.

2. Start a service without its settings:

   ```bash
   uv run gateway-serve; echo "exit code $?"
   ```

   Expect one JSON line ending `invalid configuration: SEARCH_URL is required` and `exit code 2`.

3. With the stack up, time a stop:

   ```bash
   time docker compose stop gateway
   ```

   Expect a few seconds at most, well under the 20-second grace period.

## Mistakes people make

- **Binding to `127.0.0.1` inside a container.** The port then answers only inside the container and published ports show "connection reset".
- **Binding to `0.0.0.0` on a laptop for a database-backed demo** without limiting the published port. Docker publishing on `127.0.0.1:port` is the safer pattern.
- **Starting the app through a shell without `exec`.** The shell receives `SIGTERM`, the app does not, and shutdown is not graceful.
- **Running with auto-reload in production.** `--reload` is for development.
- **Ignoring the shutdown timeout.** Containers are killed after Docker's grace period; make uvicorn's wait shorter than that.

## Check yourself

1. What does uvicorn do between your application and the network?
   Answer: it accepts HTTP connections and calls the ASGI application with each request.
2. Why `0.0.0.0` inside a container?
   Answer: so connections from outside the container (through the published port) are accepted.
3. What happens on `SIGTERM`?
   Answer: uvicorn stops accepting connections, finishes in-flight requests, runs shutdown code and exits.
4. What does `timeout_graceful_shutdown=10` mean?
   Answer: wait at most 10 seconds for in-flight requests before cancelling them.
5. Why `log_config=None`?
   Answer: so uvicorn does not replace the application's own logging configuration.

## Further reading

- uvicorn documentation (this repository pins uvicorn 0.54): https://uvicorn.dev/
- Settings, including `timeout_graceful_shutdown`: https://uvicorn.dev/settings/
- Deployment: https://uvicorn.dev/deployment/
