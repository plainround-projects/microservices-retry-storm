# import-linter

## What it is

import-linter is a tool that checks which modules import which other modules. You write the allowed dependencies as **contracts** in the configuration. The tool builds the graph of imports from your code and fails if a contract is broken. It turns an architecture rule that lives in a diagram into a check that runs on every commit.

## Why this repository uses it

The resilience policy has to stay testable without a network. If a use case imported `httpx` or `prometheus_client`, its retry decisions could no longer be tested with a fake clock in milliseconds. The tool keeps the domain and the application of each service free of the outside world.

## Where it appears

| Path | What it does |
| --- | --- |
| `services/gateway/pyproject.toml`, `search/`, `pricing/`, `stock/` | The `[tool.importlinter]` contracts for each service |
| `Makefile` | `make lint` runs `lint-imports` in each service folder; `check-package` runs it for the package under test |

The two libraries have no such contracts: `resilience` has no layers and no dependencies at all, and `chassis` is infrastructure.

## How it works underneath

- Python code depends on other code through `import` lines. import-linter reads them (without running the code) and builds a directed graph: an arrow from module A to module B means "A imports B".
- A **layers** contract lists layers from top to bottom. A module may import from layers below it, never from layers above it. This keeps the core of the program free of knowledge about the outside.
- A **forbidden** contract says "these source modules must not import these target modules", directly or through any chain of other modules.
- `include_external_packages = true` lets the contracts mention third-party packages such as `fastapi`.
- The command is `lint-imports`. It prints each contract as KEPT or BROKEN, with the import chain for any break, and exits non-zero if any contract is broken.

## Walkthrough of this repository's use

```toml
[tool.importlinter]
root_package = "gateway"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = ["gateway.interfaces", "gateway.infrastructure", "gateway.application", "gateway.domain"]

[[tool.importlinter.contracts]]
name = "Domain and application know nothing about FastAPI, PostgreSQL, httpx, Prometheus or the chassis"
type = "forbidden"
source_modules = ["gateway.domain", "gateway.application"]
forbidden_modules = ["fastapi", "psycopg", "psycopg_pool", "httpx", "prometheus_client", "chassis"]
```

- `root_package = "gateway"`: the package to analyze. `include_external_packages = true` lets the second contract name third-party packages.
- First contract, `type = "layers"`: `interfaces` (HTTP, wiring) may import `infrastructure`, which may import `application`, which may import `domain`; never the other way. The name says it: "never upward".
- Second contract, `type = "forbidden"`: `gateway.domain` and `gateway.application` must not import `fastapi`, `psycopg`, `psycopg_pool`, `httpx`, `prometheus_client` or `chassis`, even through a chain of other modules.

What the layers contain, with the gateway as the example:

| Layer | Holds | Example |
| --- | --- | --- |
| `domain` | Plain value types, standard library only | `SearchAnswer` |
| `application` | The use case and its ports (`ports.py`) | `HandleSearch`, `SearchUpstream`, `Telemetry` |
| `infrastructure` | Adapters to the outside: HTTP to search, Prometheus telemetry, settings | `HttpSearchUpstream`, `PrometheusTelemetry` |
| `interfaces` | The HTTP API, the wiring, the entry point | `create_app`, `build_app`, `main` |

The application imports `resilience` (the policy library) on purpose: the library has no input or output, so it is allowed.

## Try it yourself

1. Run the check for one service:

   ```bash
   cd services/gateway && uv run lint-imports
   ```

   Expect `Analyzed 30 files, 51 dependencies.`, both contracts `KEPT`, and `Contracts: 2 kept, 0 broken.`

2. Break a rule on purpose and watch it report the chain. Add `import httpx` after the docstring of `services/gateway/src/gateway/domain/models.py`, run the check, and restore the file:

   ```bash
   cd services/gateway && sed -i.bak '1a\
   import httpx
   ' src/gateway/domain/models.py && uv run lint-imports; mv src/gateway/domain/models.py.bak src/gateway/domain/models.py
   ```

   Expect `gateway.domain is not allowed to import httpx:` with `gateway.domain.models -> httpx (l.2)`, and a report for `gateway.application` too, which reaches `httpx` through the domain.

## Mistakes people make

- **Writing contracts and not running them in CI.** A rule that nobody checks is a wish.
- **Checking only direct imports.** A forbidden rule also catches indirect chains; read the chain it prints.
- **Forgetting the external-packages option** and wondering why a rule about `fastapi` does nothing.
- **Fixing a break with a local import** (an import inside a function) to hide it. The dependency is still there.
- **Making the layers too fine.** Four layers are enough to teach the point.

## Check yourself

1. What does a layers contract forbid?
   Answer: a lower layer importing from a higher one.
2. What does `lint-imports` exit with when a contract is broken?
   Answer: a non-zero exit code, so CI fails.
3. What does a forbidden contract express?
   Answer: that certain modules must not (even indirectly) import certain others.
4. Why does the tool not need to run the program?
   Answer: it reads the import statements from the source.
5. Why check architecture rules automatically?
   Answer: because rules in a diagram drift; a check fails the first time someone breaks them.

## Further reading

- import-linter documentation (this repository pins import-linter 2.15): https://import-linter.readthedocs.io/en/stable/
- Contract types: https://import-linter.readthedocs.io/en/stable/contract_types.html
- Usage: https://import-linter.readthedocs.io/en/stable/usage.html
