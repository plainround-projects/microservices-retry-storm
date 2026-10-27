# Bash and shell scripts

## What it is

A **shell** is a program that reads commands you type (or read from a file) and runs other programs for you. `bash` is the most common shell. `sh` is a smaller, older, more portable shell; on many systems it is a lightweight program that understands less than bash. A **shell script** is a text file of commands, run top to bottom.

## Why this repository uses it

The only script is a small one: applying the stock store's SQL when its database container starts. The rest of the shell in this repository is one-liners inside the Makefile and the container start command. The experiments themselves are Python.

## Where it appears

| Path | What it does |
| --- | --- |
| `scripts/apply-sql.sh` | Runs the stock store's SQL files in order inside the `stock-db-init` container (an `sh` script) |
| `Makefile` | Recipe lines that are small shell programs: `for` loops, `[ -f ... ] ||`, `$$(...)`, `[ -d ... ] &&` |
| `Dockerfile` | `CMD ["sh", "-c", "exec ${SERVICE}-serve"]` |
| `docker-compose.yml` | `entrypoint: ["/scripts/apply-sql.sh", "/sql"]` runs the script |

## How it works underneath

- A script starts with a **shebang** line, such as `#!/usr/bin/env bash`. The two characters `#!` tell the operating system which program should read the rest of the file. The file must also be marked executable (`chmod +x`) to be started by name.
- Every program ends with an **exit code**, a number. 0 means success; any other number means failure. The shell stores the last one in `$?`. `&&` runs the next command only if the last one succeeded; `||` runs it only if the last one failed.
- A **pipe** (`a | b`) connects the output of `a` to the input of `b`. A **redirect** (`> file`, `< file`) connects a command to a file. `>/dev/null` throws output away. `2>&1` sends error output to the same place as normal output.
- A **variable** is set with `name=value` (no spaces) and read with `$name` or `${name}`. `$(command)` runs a command and substitutes its output. `"$@"` and `"$1"` are the arguments the script was given.
- **Quotes**: inside double quotes, variables are still replaced but the text is not split into words. Without quotes, a value with spaces becomes several arguments. Single quotes keep everything literal.
- **Safety switches.** `set -e` makes the script stop at the first command that fails. `set -u` makes using an unset variable an error. `set -o pipefail` (bash only) makes a pipe fail if any part fails, not just the last. `set -euo pipefail` turns all three on.
- **Traps.** `trap 'commands' EXIT` runs the commands when the script ends, by success or failure. It is the standard way to clean up temporary files.

## Walkthrough of this repository's use

### `scripts/apply-sql.sh`

```sh
#!/bin/sh
# Apply a service's SQL in order: 001_schema, 002_seed, then migrations/*.sql. 003_queries is never applied.
# Used by the *-db-init containers and by `make db`. Reads PGHOST, PGUSER, PGPASSWORD, PGDATABASE and APP_PASSWORD.
set -eu
dir="${1:?usage: apply-sql.sh <sql-dir>}"
# wait for the database to accept queries (it can still be recovering when its health check passes)
for _ in $(seq 1 90); do
  psql --no-psqlrc -q -c "SELECT 1" >/dev/null 2>&1 && break
  sleep 2
done
```

- `#!/bin/sh` asks for the small shell, because the `postgres:18` image used by the init job has `sh`. So the script avoids bash-only features.
- `set -eu` stops on the first failure and on unset variables (plain `sh` has no `pipefail`).
- `dir="${1:?usage: apply-sql.sh <sql-dir>}"` takes the first argument; `${1:?message}` prints the message and exits if it is empty. The compose file passes `/sql`.
- `for _ in $(seq 1 90)` repeats up to 90 times; `seq 1 90` prints 1 to 90 and `_` is a loop variable we do not use. `psql --no-psqlrc -q -c "SELECT 1" >/dev/null 2>&1 && break` tries a trivial query with output thrown away; success leaves the loop, failure falls to `sleep 2`. The comment gives the reason: a database can still be recovering when its health check already passes.

```sh
run() {
  echo "applying $1"
  psql --no-psqlrc -v ON_ERROR_STOP=1 -v app_password="${APP_PASSWORD:?}" -q -f "$1"
}
for file in "$dir"/00[0-9]_*.sql; do
  case "$file" in *_queries.sql) continue ;; esac
  run "$file"
done
for file in "$dir"/migrations/*.sql; do
  [ -e "$file" ] || continue
  run "$file"
done
```

- `run() { ... }` defines a function; `$1` inside is its first argument. `-v ON_ERROR_STOP=1` makes `psql` fail at the first SQL error. `-v app_password="${APP_PASSWORD:?}"` passes the application role's password into SQL; `:?` fails if it is empty. `-f "$1"` runs the file.
- `"$dir"/00[0-9]_*.sql` is a **glob**: it matches `001_schema.sql`, `002_seed.sql`, `003_queries.sql` in sorted order. `case "$file" in *_queries.sql) continue ;; esac` skips the read-only queries file.
- The second loop runs `migrations/*.sql`; `[ -e "$file" ] || continue` skips when there are none (an unmatched glob stays as literal text). This service has no migrations folder, so the loop does nothing.

### Shell inside the Makefile

```make
env:
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }

install: ## create .venv from uv.lock
	uv sync --all-packages

up: env install ## build the images and start the stock database and the four services
	$(COMPOSE) up -d --build --wait --wait-timeout 1200

db: ## apply the stock store's sql/ again (schema, seed)
	$(COMPOSE) run --rm stock-db-init
```

`[ -f .env ] || { cp .env.example .env; echo ...; }` means "if `.env` is not a file, copy the example and say so". `{ ...; }` groups commands. In `install` and `up` the shell is only a launcher. See `make.md`.

```make
# Unit tests need no services. The domain and application of every service, and all of resilience, stay at 95% or more.
test: install ## unit tests with coverage for every package, and the design page against the code (no services needed)
	@for p in $(PACKAGES); do \
	  name=$$(basename $$p); \
	  (cd $$p && echo "== unit tests $$p" && uv run pytest tests/unit -m "not integration" --cov=$$name --cov-report=term-missing:skip-covered -q \
	    && if [ -d src/$$name/domain ]; then uv run coverage report --include="src/$$name/domain/*,src/$$name/application/*" --fail-under=95; fi \
	    && if [ $$name = resilience ]; then uv run coverage report --fail-under=95; fi) || exit 1; \
	done
	uv run pytest tests/system -q
```

This recipe is a shell program inside make. `for p in $(PACKAGES); do ... done` loops over folders (make fills in the list; `$$p` is the shell variable). `name=$$(basename $$p)` captures the folder's last name. `( ... ) || exit 1` runs the group in a subshell so the `cd` does not leak, and stops at the first failure. `&&` chains: run the coverage check only if the tests passed. `if [ -d src/$$name/domain ]; then ...; fi` tests whether a folder exists. `if [ $$name = resilience ]` compares two strings (`[ ... ]` is the `test` command).

### The container's start command

```dockerfile
CMD ["sh", "-c", "exec ${SERVICE}-serve"]
```

A shell expands `${SERVICE}` and `exec` replaces itself with `stock-serve` (or `gateway-serve` and so on). The service becomes process 1 and receives `SIGTERM` directly (see `uvicorn.md`).

## Try it yourself

1. Check the script's syntax without running it:

   ```bash
   sh -n scripts/apply-sql.sh && echo "syntax ok"
   ```

   Expect `syntax ok`.

2. See the required-argument check fire:

   ```bash
   sh scripts/apply-sql.sh; echo "exit code $?"
   ```

   Expect a message ending in `usage: apply-sql.sh <sql-dir>` and `exit code 1`.

3. See what the glob matches:

   ```bash
   ls services/stock/sql/00[0-9]_*.sql
   ```

   Expect `services/stock/sql/001_schema.sql`, `002_seed.sql`, `003_queries.sql`.

4. See `pipefail` change a result:

   ```bash
   bash -c 'set -eu; false | true; echo "carried on"'; bash -c 'set -euo pipefail; false | true; echo "not printed"'
   ```

   The first prints `carried on`; the second prints nothing.

## Mistakes people make

- **Leaving variables unquoted.** A path with a space breaks into two arguments.
- **No `set -e`.** The script carries on after a failed step and reports success.
- **Using bash features in an `sh` script.** `pipefail`, `[[ ]]` and arrays are not guaranteed in `sh`.
- **Parsing `ls` output.** File names can contain anything; use a `for file in pattern` loop.
- **Forgetting that a pipe hides failures** without `pipefail`: `false | true` succeeds.
- **Piping to `grep` under `set -e`.** `grep` exits 1 when it finds nothing; that stops the script unless you add `|| true` or handle it.

## Check yourself

1. What does `set -euo pipefail` do?
   Answer: stop at the first failure, treat unset variables as errors, and make a pipeline fail if any stage fails.
2. What is `$?`?
   Answer: the exit code of the last command; 0 is success.
3. Why quote `"$dir"`?
   Answer: so a value containing spaces stays one argument.
4. What does `trap '...' EXIT` do?
   Answer: runs the commands when the script ends, however it ends; used to delete temporary files.
5. What is the difference between `a && b` and `a || b`?
   Answer: `b` runs after a success of `a` for `&&`, and after a failure of `a` for `||`.

## Further reading

- GNU Bash reference manual (this repository runs bash 3.2 or newer; macOS and Linux CI both work): https://www.gnu.org/software/bash/manual/bash.html
- POSIX shell command language (what `sh` guarantees): https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html
