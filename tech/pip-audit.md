# pip-audit

## What it is

pip-audit checks the Python packages a project uses against a database of known security vulnerabilities. Each known problem has an identifier, such as a CVE number or a PYSEC or GHSA id. If a pinned version has a known problem, pip-audit prints it and fails.

## Why this repository uses it

Four services will run in containers built from the lock file. The audit checks that exact set of packages, so a vulnerable pin is found in CI before an image is built.

## Where it appears

| Path | What it does |
| --- | --- |
| `Makefile` (`audit`) | Exports the locked list and audits it |
| `.github/workflows/ci.yml` (job `audit`) | Runs `make audit` on every push |
| `pyproject.toml` (root) | Pins `pip-audit>=2.7` in the dev tools |

## How it works underneath

- You give it a list of exact `name==version` pairs. Here it comes from `uv export` (see `uv.md`) written to a temporary file and passed with `-r`.
- For each pair, pip-audit asks a vulnerability service (by default the Python Packaging Advisory Database through PyPI) whether that version is affected, and which version fixes it.
- `--no-deps` says "audit exactly this list and do not work out dependencies yourself" (the list already includes them all). `--disable-pip` says "do not run pip to resolve anything". Together they make it a pure lookup of a known list.
- The exit code is 0 when nothing is found and 1 when something is. That makes it usable as a CI gate.
- It needs network access to the service, so it is not part of the offline checks.

## Walkthrough of this repository's use

```make
audit: install ## check the locked dependencies for known vulnerabilities
	uv export --frozen --all-packages --no-emit-workspace --no-hashes -q > /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt
	uv run pip-audit --no-deps --disable-pip -r /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt
```

1. `audit: install` makes sure the environment exists.
2. `uv export --frozen --all-packages --no-emit-workspace --no-hashes -q > /tmp/$(COMPOSE_PROJECT_NAME)-audit.txt` writes the locked list. `COMPOSE_PROJECT_NAME` comes from `.env`, so the file is `/tmp/<project name>-audit.txt`; the CI job copies `.env.example` to `.env` first for that reason. `--no-emit-workspace` leaves out `chassis`, `resilience`, `gateway`, `search`, `pricing`, `stock`, which are not on PyPI. `--no-hashes` keeps the lines short; the audit is by version.
3. `uv run pip-audit --no-deps --disable-pip -r /tmp/...txt` audits the list.

```yaml
  audit:
    name: Dependency audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.11.2"
      - run: uv sync --frozen --all-packages --python 3.12
      - run: cp .env.example .env && make audit
```

The `audit` job is separate from the per-package job so that a new advisory fails one clearly named job.

## Try it yourself

1. Run the audit (needs network):

   ```bash
   make audit
   ```

   Expect a warning line from pip-audit about hashes and then `No known vulnerabilities found`. Exit code 0.

2. Look at the list it audits:

   ```bash
   head -6 /tmp/*-audit.txt
   ```

   Expect a comment header and the first pins, such as `annotated-doc==0.0.5`, each with a `# via` line. The list includes `prometheus-client==0.26.0`.

3. Audit one old package on purpose, to see a finding. Make a throwaway list with a very old version (this is not part of the repository):

   ```bash
   printf 'requests==2.5.0\n' > /tmp/old.txt && uv run pip-audit --no-deps --disable-pip -r /tmp/old.txt; echo "exit code $?"
   ```

   Expect a table of vulnerabilities for `requests`, with the fixed versions, and `exit code 1`.

## Mistakes people make

- **Auditing the wrong list.** Auditing your laptop's global packages says nothing about the project. Audit the lock file.
- **Ignoring findings because "it is only a dev tool".** Dev tools run on your machine and in CI.
- **Treating "no known vulnerabilities" as "secure".** It only means the database knows nothing yet.
- **Pinning forever.** Re-run the audit regularly, because new problems are published for old versions.
- **Running it in an offline environment** and reading the network error as a pass.

## Check yourself

1. What does pip-audit compare?
   Answer: the exact versions in your list against a database of known vulnerabilities.
2. Why feed it the lock file?
   Answer: because the lock file is what actually gets installed, including indirect dependencies.
3. What does exit code 1 mean here?
   Answer: at least one known vulnerability was found.
4. Does "No known vulnerabilities found" prove the code is safe?
   Answer: no, only that nothing in the database matches.
5. Why does it need the network?
   Answer: it asks a remote service for the advisories.

## Further reading

- pip-audit documentation (this repository pins pip-audit 2.10): https://pypi.org/project/pip-audit/
- Source and options: https://github.com/pypa/pip-audit
- The Python Packaging Advisory Database: https://github.com/pypa/advisory-database
