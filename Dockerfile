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

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS dependencies
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=ghcr.io/astral-sh/uv@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY --from=manifests /m/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace --all-packages

FROM dependencies AS build
ARG SERVICE
COPY libs ./libs
COPY services ./services
# No shared cache here: images built in parallel would wait on each other for the cache lock.
RUN uv pip install --no-cache --python /app/.venv/bin/python --no-deps ./libs/* "./services/${SERVICE}"

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
