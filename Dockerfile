# One image per service, selected with --build-arg SERVICE=<folder under services/>. The build context is the repository root
# because every service installs the shared libraries from libs/.
#
# Dependencies are installed in their own layer, from the lock file and the packages' manifests only, so a change to
# source code does not download them again.
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS manifests
COPY libs /m/libs
COPY services /m/services
RUN find /m -type f ! -name pyproject.toml -delete && find /m -type d -empty -delete

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS build
ARG SERVICE
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install uv==0.11.2
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY --from=manifests /m/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace --package "${SERVICE}"
COPY libs ./libs
COPY services ./services
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --package "${SERVICE}"

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
