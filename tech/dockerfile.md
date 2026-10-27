# Dockerfile syntax

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

A Dockerfile is a text file with the recipe for building a Docker image (see `docker.md`). Each line is an instruction in capital letters followed by arguments. Docker runs them top to bottom; most of them add a layer to the image.

## Why this repository uses it

One file builds the image for any service in the workspace. `SERVICE=gateway`, `search`, `pricing` or `stock` selects which one, so the four services are built the same way and cannot drift apart. The file is arranged so a change to application code does not download the dependencies again.

## Where it appears

| Path | What it does |
| --- | --- |
| `Dockerfile` | Four stages, one final image per service |
| `docker-compose.yml` | `build: { context: ., args: { SERVICE: stock } }` selects the service |
| `.dockerignore` | What the build context leaves out |

## How it works underneath

The instructions this repository uses:

- `FROM image AS name` starts a new **stage** from a base image. A Dockerfile with several `FROM` lines is a **multi-stage build**: later stages can copy files out of earlier ones, and only the last stage becomes the final image. Everything else is thrown away, which keeps the final image small.
- `ARG name` declares a build-time variable, set with `--build-arg name=value`. `ENV name=value` sets an environment variable that exists in the running container.
- `WORKDIR path` sets the folder for the following instructions.
- `COPY src dest` copies files from the build context. `COPY --from=stage-or-image src dest` copies from an earlier stage or from another image.
- `RUN command` runs a command while building and saves the result as a layer. `RUN --mount=type=cache,target=...` gives that command a persistent cache folder that is not stored in the image (a BuildKit feature).
- `USER name` makes later instructions and the final process run as that user.
- `EXPOSE port` documents the port the program listens on. It does not publish anything.
- `HEALTHCHECK` defines the command Docker runs to decide whether the container is healthy.
- `CMD ["a", "b"]` is the command the container runs by default. The **exec form** (a JSON list) runs the program directly. The **shell form** (a plain string) runs it through `/bin/sh -c`.
- A line ending in `\` continues on the next line. `#` starts a comment.

## Walkthrough of this repository's use

### Stage 1, `manifests`

```dockerfile
# One image per service, selected with --build-arg SERVICE=<folder under services/>. The build context is the repository root
# because every service installs the shared libraries from libs/.
#
# The third-party dependencies of every package in the workspace are installed once, in their own layer, from the lock
# file and the packages' manifests only. Every service image reuses that layer and adds only its own source, so a change to
# code does not download dependencies again and building three services does not install them three times.
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS manifests
COPY libs /m/libs
COPY services /m/services
RUN find /m -type f ! -name pyproject.toml -delete && find /m -type d -empty -delete
```

- `FROM python:3.12.8-slim-bookworm@sha256:...` is the base: Python 3.12.8 on Debian "bookworm", slim variant. The `@sha256:...` digest pins one exact image; even if the tag moves, this build does not change.
- `COPY libs /m/libs` and `COPY services /m/services` bring in the folders. The `RUN find ... -delete` line then keeps only the `pyproject.toml` files: `find /m -type f ! -name pyproject.toml -delete` deletes every file that is not named `pyproject.toml`, and `find /m -type d -empty -delete` removes folders left empty. What remains is the skeleton of the workspace with just its manifests.

### Stage 2, `dependencies`

```dockerfile
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS dependencies
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=ghcr.io/astral-sh/uv@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY --from=manifests /m/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace --all-packages
```

- `ENV UV_LINK_MODE=copy ...` configures uv (see `uv.md`).
- `COPY --from=ghcr.io/astral-sh/uv@sha256:... /uv /usr/local/bin/uv` copies the single `uv` program out of uv's own image; no `pip install` needed.
- `WORKDIR /src`, then copy the root `pyproject.toml` and `uv.lock`, then the manifests from stage 1.
- `RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace --all-packages` installs every third-party dependency of every package. This layer depends only on the manifests and the lock file, so it is rebuilt only when they change. The cache mount keeps uv's download cache between builds.

### Stage 3, `build`

```dockerfile
FROM dependencies AS build
ARG SERVICE
COPY libs ./libs
COPY services ./services
# No shared cache here: images built in parallel would wait on each other for the cache lock.
RUN uv pip install --no-cache --python /app/.venv/bin/python --no-deps ./libs/* "./services/${SERVICE}"
```

`FROM dependencies AS build` starts from stage 2. `ARG SERVICE` receives the build argument. The source is copied now, because it changes most often. `uv pip install --no-cache --python /app/.venv/bin/python --no-deps ./libs/* "./services/${SERVICE}"` installs our own packages without touching dependencies. The comment records a lesson learned: a shared cache here made parallel builds wait on a lock.

### Stage 4, the final image

```dockerfile
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
ARG SERVICE
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/app/.venv/bin:$PATH SERVICE=${SERVICE}
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=build /app/.venv /app/.venv
USER app
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=15s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3).status == 200 else 1)"]
CMD ["sh", "-c", "exec ${SERVICE}-serve"]
```

- `ARG SERVICE` again (each stage starts fresh), and `ENV` sets four variables: `PYTHONUNBUFFERED=1` flushes output at once so logs are not delayed, `PYTHONDONTWRITEBYTECODE=1` stops Python writing `.pyc` files into a read-only file system, `PATH=/app/.venv/bin:$PATH` puts the environment's commands first, and `SERVICE=${SERVICE}` keeps the name for the `CMD`.
- `RUN useradd --system --uid 10001 --no-create-home app` creates an unprivileged user. `USER app` switches to it for everything after, including the running service.
- `COPY --from=build /app/.venv /app/.venv` is the only thing taken from the build stages: the finished environment. Compilers, caches and sources are left behind.
- `EXPOSE 8080` documents the port.
- `HEALTHCHECK --interval=15s --timeout=15s --start-period=20s --retries=5 CMD [...]` runs a short Python program every 15 seconds: it fetches `http://127.0.0.1:8080/health/ready` and exits 0 only if the status is 200. `--start-period=20s` means failures in the first 20 seconds do not count. Five failures in a row mark the container unhealthy. `python -c "..."` is used because the slim image has no `curl`.
- `CMD ["sh", "-c", "exec ${SERVICE}-serve"]` runs a shell only to expand the variable and then `exec`s the service, which becomes process 1 and receives `SIGTERM` (see `bash.md`, `uvicorn.md`).

## Try it yourself

These need Docker running.

1. Build the stock image:

   ```bash
   docker build --build-arg SERVICE=stock -t storm-stock:try .
   ```

   Expect the build to run the four stages and end with `naming to ... storm-stock:try`. The first build is slow; a second build with no changes finishes in seconds because every layer is cached.

2. Check that it is not root:

   ```bash
   docker run --rm --entrypoint id storm-stock:try
   ```

   Expect `uid=10001(app) gid=...`.

3. Look at the layers and their sizes:

   ```bash
   docker history storm-stock:try
   ```

   Expect one line per layer, with the large one being the copied virtual environment.

4. Remove the image:

   ```bash
   docker rmi storm-stock:try
   ```

## Mistakes people make

- **Copying everything before installing dependencies,** which destroys the cache.
- **Using the shell form of `CMD`** and finding that the program does not receive `SIGTERM`.
- **Multiple `RUN` lines for one job** when a single line with `&&` would make one layer.
- **Forgetting that `ENV` and `ARG` have different lifetimes:** `ARG` ends after the build.
- **Leaving the build tools in the final image.** Multi-stage builds exist to avoid that.
- **Running as root** by not setting `USER`.

## Check yourself

1. What is a multi-stage build for?
   Answer: build in one stage, copy only the results into a small final stage.
2. What is the difference between `ARG` and `ENV`?
   Answer: `ARG` exists only during the build; `ENV` also exists in the running container.
3. Does `EXPOSE 8080` publish the port?
   Answer: no, it only documents it.
4. Why is `CMD ["a", "b"]` (exec form) preferred?
   Answer: the program runs directly and receives signals.
5. Why does instruction order matter?
   Answer: layers are cached; put what changes least first.

## Further reading

- Dockerfile reference: https://docs.docker.com/reference/dockerfile/
- Multi-stage builds: https://docs.docker.com/build/building/multi-stage/
- Build best practices: https://docs.docker.com/build/building/best-practices/
