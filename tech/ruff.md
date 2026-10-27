# ruff

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

ruff is a tool that reads Python source files without running them. It does two jobs. As a **linter** (`ruff check`) it finds likely mistakes and style problems, such as an unused import or a missing docstring. As a **formatter** (`ruff format`) it rewrites files into one consistent layout, so nobody argues about spaces and line breaks. It is written in Rust and runs in a fraction of a second.

## Why this repository uses it

Six packages and the root `tests/` and `scripts/` must read as one codebase. One tool with one configuration at the root applies to all of them, and CI fails on a style problem so that no reviewer has to.

## Where it appears

| Path | What it does with ruff |
| --- | --- |
| `pyproject.toml` (root) | The `[tool.ruff]` settings for the whole repository |
| `Makefile` | `make lint` runs `ruff check .` and `ruff format --check .`; `make format` fixes |
| `.github/workflows/ci.yml` | `make check-package` runs both commands per package, and the `root` job runs them for the whole root |

## How it works underneath

- ruff parses each file into a syntax tree and applies **rules**. Each rule has a code: a letter prefix for the family and a number. `F401` means "imported but unused" (family `F`, from the Pyflakes tool). `E501` is a line that is too long. `I001` is badly sorted imports.
- The configuration lists which families to turn on (`select`) and which individual rules to turn off (`ignore`), per project or per file (`per-file-ignores`).
- Some problems are **fixable**: `ruff check --fix` edits the file for you.
- `ruff format --check` changes nothing; it exits with an error if a file would be reformatted. That is what CI uses.
- The exit code is 0 when everything is clean and 1 when there are findings, so scripts and CI can react.

## Walkthrough of this repository's use

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
extend-exclude = ["tech"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "N", "C4", "RUF", "PT", "D100", "D101", "D103", "D104"]
# Named errors read better without a suffix (DeadlineTooShort, Overloaded).
ignore = ["N818"]

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["D", "E501"]
"tests/**" = ["D", "E501"]
"scripts/**" = ["D", "E501"]

[tool.ruff.lint.isort]
known-first-party = ["chassis", "resilience", "gateway", "search", "pricing", "stock"]
```

- `line-length = 120`: the width the formatter wraps to and the `E501` rule enforces. `target-version = "py312"`: use 3.12 syntax and rules. `extend-exclude = ["tech"]`: skip the `tech/` notes, because ruff 0.17 also formats Python code blocks inside Markdown and these notes quote fragments that are not complete programs.
- `select` turns on these rule families:
  - `E`, `F`: pycodestyle errors and Pyflakes (undefined names, unused imports).
  - `I`: import sorting.
  - `B`: bugbear, patterns that are probably bugs.
  - `UP`: upgrade old syntax to the target version.
  - `SIM`: simplifiable code.
  - `N`: naming conventions.
  - `C4`: better comprehensions.
  - `RUF`: ruff's own rules.
  - `PT`: pytest style.
  - `D100`, `D101`, `D103`, `D104`: a docstring is required on every public module, class, function and package. Only these four docstring rules are on.
- `ignore = ["N818"]` turns off the rule that wants exception names to end in `Error`; the comment says why (`DeadlineTooShort` and `Overloaded` read better).
- `per-file-ignores` removes docstring rules (`D`) and the line-length rule (`E501`) for tests and scripts.
- `known-first-party` lists our package names so the import sorter groups them apart from third-party imports. Look at the top of `libs/chassis/src/chassis/upstream.py`: standard library, then third party (`httpx`), then first party (`chassis...`, `resilience...`), each group separated by a blank line.

```make
lint: install ## ruff, formatting, mypy and import rules for every package
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	@for p in $(PACKAGES); do (cd $$p && echo "== mypy $$p" && uv run mypy) || exit 1; done
	@for s in $(SERVICES); do (cd services/$$s && echo "== import rules $$s" && uv run lint-imports) || exit 1; done

format: install ## apply ruff's formatting and safe fixes
	uv run ruff check --fix .
	uv run ruff format .
```

`make lint` is the checking form; `make format` repairs: `ruff check --fix .` then `ruff format .`.

## Try it yourself

1. Check everything:

   ```bash
   uv run ruff check . && uv run ruff format --check .
   ```

   Expect `All checks passed!` and `N files already formatted`.

2. Make a problem and watch ruff find it:

   ```bash
   printf 'import os\n' > scratch_demo.py; uv run ruff check scratch_demo.py; rm scratch_demo.py
   ```

   Expect `D100 Missing docstring in public module` and `F401 [*] 'os' imported but unused`, then `Found 2 errors.`

3. Ask for an explanation of a rule:

   ```bash
   uv run ruff rule F401 | head -4
   ```

   Expect `# unused-import (F401)` and a line saying it is derived from the Pyflakes linter.

## Mistakes people make

- **Silencing a rule with `# noqa` instead of reading it.** A suppression hides the finding but not the problem.
- **Running the formatter and the linter in a way that fights.** Run `ruff check --fix` and then `ruff format`; both are used together in this repository.
- **Different versions on different machines.** Pin it in the lock file, as this repository does.
- **Formatting in the same commit as a real change,** which makes the diff hard to read.
- **Turning on every rule at once** and drowning in findings. Add families as the code is ready.

## Check yourself

1. What is the difference between `ruff check` and `ruff format`?
   Answer: `check` finds problems (the linter); `format` rewrites layout (the formatter).
2. What does `F401` mean?
   Answer: an import that is never used.
3. What does `ruff format --check` do?
   Answer: reports files that would change, and exits non-zero, without changing them.
4. How do you apply the automatic fixes?
   Answer: `ruff check --fix`.
5. Where do you turn rules on and off?
   Answer: `select`, `ignore` and `per-file-ignores` in `[tool.ruff.lint]` in `pyproject.toml`.

## Further reading

- ruff documentation (this repository pins ruff 0.17): https://docs.astral.sh/ruff/
- Rules: https://docs.astral.sh/ruff/rules/
- Configuration: https://docs.astral.sh/ruff/configuration/
- Formatter: https://docs.astral.sh/ruff/formatter/
