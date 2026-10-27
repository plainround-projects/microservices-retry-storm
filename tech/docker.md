# Docker

## What it is

Docker is a tool that runs programs inside **containers**. A container is a normal process on your machine, but Docker gives it its own private view: its own file system, its own network name, its own list of processes, and limits on what it may use. It is lighter than a virtual machine because it shares the host's operating system kernel (on a Mac or Windows laptop, Docker Desktop runs a small Linux virtual machine and starts the containers inside it).

An **image** is the read-only package a container starts from: a base file system plus your files and settings. One image can start many containers.

## Why this repository uses it

The experiments need four services and a database that can be restarted, slowed and stopped at will, and then thrown away. Containers give each piece its own process and network name, and `docker compose down -v` leaves nothing behind. The outage experiment literally stops the stock container; the shedding experiment recreates the search container with a new limit.

## Where it appears

| Path | What it does with Docker |
| --- | --- |
| `Dockerfile` | Builds one image per service (see `dockerfile.md`) |
| `docker-compose.yml` | Describes the chain (see `docker-compose.md`) |
| `.dockerignore` | Keeps `.git`, caches, `.env` and `docs` out of the build context |
| `tests/system/harness.py`, `experiments.py` | Run `docker compose` to restart, stop and recreate services during experiments |
| `Makefile` | `docker compose up/down/run/exec/logs` behind short names |
| `.github/workflows/ci.yml` | The `system` job runs the stack on a CI machine |

## How it works underneath

- An image is built from **layers**. Each instruction in a Dockerfile (see `dockerfile.md`) adds a layer. Docker caches layers: if an instruction and everything above it are unchanged, the cached layer is reused. That is why instructions that change rarely come first.
- The **build context** is the folder whose files a build may copy. A `.dockerignore` file lists what to leave out of it, which keeps builds fast and keeps secrets and the `.git/` folder out of images.
- `docker run` makes a container from an image. A container has a **writable layer** on top of the image, which disappears when the container is removed. Data that must survive lives in a **volume**.
- A container reaches other containers by **name** on a private network that Docker Compose creates (see `docker-compose.md`). To be reachable from your laptop, a container port is **published**: `127.0.0.1:9000:8080` means "laptop port 9000, only from this machine, goes to container port 8080".
- A **registry** is a server that stores images. `postgres:18` means "image `postgres`, tag `18`" from the default registry. A digest (`@sha256:...`) names one exact image, even if the tag later points elsewhere.
- When you stop a container, Docker sends the main process (process number 1 in the container) the signal `SIGTERM`, waits for a **grace period**, then sends `SIGKILL`, which cannot be caught.
- A **health check** is a command Docker runs inside the container on a schedule; its exit code marks the container healthy or unhealthy.

## Walkthrough of this repository's use

### The build context

```text
.git
.venv
**/__pycache__
**/*.egg-info
**/.pytest_cache
**/.mypy_cache
**/.ruff_cache
.env
docs
```

Each line is a pattern for files that are not sent to the builder. `.git` and `.venv` are large and irrelevant, `**/__pycache__` matches those folders at any depth, `.env` keeps local passwords out, and `docs` holds the committed experiment results, which the image does not need.

### Hardening in the compose file

```yaml
x-service: &service
  restart: on-failure:10
  read_only: true
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  stop_grace_period: 20s
  tmpfs: [/tmp]
```

These settings apply to the four service containers (see `yaml.md` for the anchor):

- `restart: on-failure:10` restarts a container that exits with an error, at most ten times.
- `read_only: true` makes the container's file system read-only; `tmpfs: [/tmp]` gives it one in-memory scratch directory.
- `cap_drop: [ALL]` removes every Linux capability; the services need none.
- `security_opt: ["no-new-privileges:true"]` forbids gaining rights through a setuid program.
- `stop_grace_period: 20s` is the time between `SIGTERM` and `SIGKILL` (see `uvicorn.md`).

### Docker driven from Python

```python
def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=600
```

The experiments call the `docker` program with `subprocess.run`: a list of arguments, `cwd=ROOT` so Compose finds the file, `capture_output=True` to collect output, `check=False` so a non-zero exit code is returned, not raised, and a timeout. Examples from `experiments.py`: `compose("stop", "stock")` and later `compose("start", "stock")` for the outage; `compose("up", "-d", "--force-recreate", "--no-deps", "search")` to restart one service with a new setting without touching its dependencies.

### Cleanup

```make
logs: ## follow the service logs
	$(COMPOSE) logs -f --no-log-prefix gateway search pricing stock

down: ## stop everything and delete the database
	$(COMPOSE) down -v --remove-orphans

clean: down
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .import_linter_cache .coverage
```

`docker compose down -v --remove-orphans` stops and removes the containers and the network. `-v` also removes the volume (the stock database); `--remove-orphans` removes containers of services no longer in the file.

## Try it yourself

These need Docker running.

1. See the versions:

   ```bash
   docker --version && docker compose version
   ```

   Expect two version lines.

2. After `make up`, list this project's containers:

   ```bash
   docker compose ps -a --format "table {{.Name}}\t{{.Status}}"
   ```

   Expect the database and the four services `Up ... (healthy)` and `storm-stock-db-init-1` `Exited (0)`.

3. Look at the hardening from inside:

   ```bash
   docker compose exec stock sh -c 'id; touch /x 2>&1; touch /tmp/ok && echo "tmp is writable"'
   ```

   Expect `uid=10001(app)`, a `Read-only file system` error for `/x`, and `tmp is writable`.

4. Stop the bottom of the chain, as the outage experiment does, and start it again:

   ```bash
   docker compose stop stock && docker compose ps stock; docker compose start stock
   ```

   Expect `stock` to be absent or exited in the first listing and healthy again a few seconds after the start.

5. Stop everything and delete the database:

   ```bash
   make down
   ```

## Mistakes people make

- **Running the container as root** when it does not need to.
- **Putting secrets in the image** (they stay in its layers). Pass them at run time.
- **Ignoring layer order.** Copying all source before installing dependencies makes every code change reinstall everything.
- **Using `latest` or a floating tag** and getting a different base image tomorrow.
- **Expecting data to survive** `docker rm` without a volume.
- **Binding a published port to all interfaces** (`8080:8080`) on a shared network. Use `127.0.0.1:` for local-only.

## Check yourself

1. What is the difference between an image and a container?
   Answer: an image is the read-only package; a container is a running (or stopped) instance of it.
2. Why put rarely-changing instructions first in a Dockerfile?
   Answer: layers are cached, so a change invalidates only the layers below it.
3. What does `-p 127.0.0.1:9000:8080` mean?
   Answer: laptop port 9000, reachable only from this machine, goes to container port 8080.
4. What happens when you run `docker stop`?
   Answer: SIGTERM, a grace period, then SIGKILL.
5. Where does data that must outlive a container go?
   Answer: into a volume.

## Further reading

- Docker documentation: https://docs.docker.com/
- Get started (images, containers, volumes): https://docs.docker.com/get-started/docker-overview/
- Build cache: https://docs.docker.com/build/cache/
- The `.dockerignore` file: https://docs.docker.com/build/concepts/context/#dockerignore-files
