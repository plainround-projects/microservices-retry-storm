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

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

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

lint: ## ruff, formatting, mypy and import rules for every package
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	@for p in $(PACKAGES); do (cd $$p && echo "== mypy $$p" && uv run mypy) || exit 1; done
	@for s in $(SERVICES); do (cd services/$$s && echo "== import rules $$s" && uv run lint-imports) || exit 1; done

format: ## apply ruff's formatting and safe fixes
	uv run ruff check --fix .
	uv run ruff format .

# Unit tests need no services. The domain and application of every service, and all of resilience, stay at 95% or more.
test: ## unit tests with coverage for every package, and the design page against the code (no services needed)
	@for p in $(PACKAGES); do \
	  name=$$(basename $$p); \
	  (cd $$p && echo "== unit tests $$p" && uv run pytest tests/unit -m "not integration" --cov=$$name --cov-report=term-missing:skip-covered -q \
	    && if [ -d src/$$name/domain ]; then uv run coverage report --include="src/$$name/domain/*,src/$$name/application/*" --fail-under=95; fi \
	    && if [ $$name = resilience ]; then uv run coverage report --fail-under=95; fi) || exit 1; \
	done
	uv run pytest tests/system -q

test-integration: ## the stock store's adapter against its database (make up first)
	cd services/stock && uv run pytest tests/integration -m integration -q

test-system: ## the chain across the running containers: every profile answers, shed and degraded paths (make up first)
	uv run pytest tests/e2e -m e2e -q

audit: ## check the locked dependencies for known vulnerabilities
	uv export --frozen --all-packages --no-emit-workspace --no-hashes -q > /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt
	uv run pip-audit --no-deps --disable-pip -r /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt

check: lint test ## everything that needs no services

check-package: ## CI: format, lint, types, import rules and unit tests for one package (PKG=services/search)
	uv run ruff check $(PKG)
	uv run ruff format --check $(PKG)
	cd $(PKG) && uv run mypy
	@if [ -d $(PKG)/src/$(notdir $(PKG))/domain ]; then cd $(PKG) && uv run lint-imports; fi
	cd $(PKG) && uv run pytest tests/unit -m "not integration" -q

logs: ## follow the service logs
	$(COMPOSE) logs -f --no-log-prefix gateway search pricing stock

down: ## stop everything and delete the database
	$(COMPOSE) down -v --remove-orphans

clean: down
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .import_linter_cache .coverage
