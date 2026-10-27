# Git

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

Git is a tool that records the history of a folder. Each saved state is a **commit**: a snapshot of every tracked file plus a message, an author, a time and a pointer to the commit before it. A folder with its history is a **repository**. Git keeps its data in a hidden `.git/` folder.

## Why this repository uses it

The history is a record of how the system was built: the policy library first, then the chassis, the four services from the bottom of the chain up, then compose and CI, then the experiments and the notes. Reading `git log` from the bottom is a reasonable way to read the repository in the order it was made.

## Where it appears

| Path | What it does with git |
| --- | --- |
| `.gitignore` | Keeps the virtual environment, caches and `.env` out of history |
| `.editorconfig` | Editor rules for indentation and line endings |
| `.dockerignore` | Not git's, but the same idea for the Docker build context (see `docker.md`) |
| `.github/workflows/ci.yml` | Runs on every push and pull request (see `github-actions.md`) |
| `docs/results/` | The measured results of the experiments are committed on purpose, as evidence |

## How it works underneath

- `git add` chooses which changes go into the next commit; `git commit` records them. A commit has an id (a long hexadecimal hash) that depends on its content and its parent, so history cannot be altered unnoticed.
- A **branch** is a movable name that points to a commit. A **tag** is a name that stays on one commit: a fixed bookmark such as `step-4-routing`.
- `git log --oneline` lists commits. `git show <name>` shows one. `git status` shows what changed.
- `git archive <name>` writes the files of one commit as an archive without the `.git/` folder; piped to `tar -x`, it unpacks a clean copy of that moment.
- A **`.gitignore`** file lists patterns of files git should not track: build output, caches, secrets. `git check-ignore -v <path>` tells you which line ignores a path.
- A **`.editorconfig`** file is not git's; editors read it to apply the same indentation and line ending rules to every contributor.

## Walkthrough of this repository's use

### `.gitignore`

```text
.venv/
__pycache__/
*.pyc
*.egg-info/
build/
dist/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.import_linter_cache/
.coverage
.env
.DS_Store
logs/
.omc/
```

Each line is a pattern. `.venv/` (with a slash) matches a folder; `*.pyc` matches by name. `.env` keeps the real passwords file out of history; `.env.example`, with throwaway values, is committed and copied by `make up`. The caches are tool output. `.omc/` is a local tool's state folder. Note what is **not** ignored: `docs/results/`, whose JSON and text files record what the experiments measured.

### `.editorconfig`

```ini
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4

[*.{yml,yaml,json,toml}]
indent_size = 2

[Makefile]
indent_style = tab

[*.md]
trim_trailing_whitespace = false
```

`root = true` stops editors from looking in parent folders. `[*]` applies to all files: UTF-8, LF line endings, a final newline, no trailing spaces, four-space indent. `[*.{yml,yaml,json,toml}]` overrides the indent to 2. `[Makefile]` requires real tabs (see `make.md`). `[*.md]` keeps trailing spaces, which Markdown uses for line breaks.

### The history

From the bottom of `git log --oneline`: `feat(resilience): deadline, backoff with jitter, retry budget, circuit breaker, concurrency limiter`, then `build: the chassis library ...`, `feat(stock)`, `feat(pricing)`, `feat(search)`, `feat(gateway)`, `build: compose, one image per service, Makefile and CI`, `test: ...experiments...`, `docs: architecture, decision records and changelog`. The prefixes (`feat`, `build`, `test`, `docs`, `fix`) follow the conventional-commits convention: a type, the area in parentheses, a colon and a summary.

## Try it yourself

1. Read the history from the beginning:

   ```bash
   git log --oneline --reverse | head -8
   ```

   Expect eight lines; the first starts `feat(resilience): deadline, backoff with jitter ...`.

2. Ask which rule ignores a path:

   ```bash
   git check-ignore -v .venv/x .env
   ```

   Expect `.gitignore:1:.venv/	.venv/x` and `.gitignore:12:.env	.env`.

3. Confirm that the measured results are tracked:

   ```bash
   git ls-files docs/results | head -4
   ```

   Expect names such as `docs/results/amplification-deadline-retry.json`.

4. Confirm that nothing generated is tracked:

   ```bash
   git ls-files | grep -E "(^|/)(\.venv|__pycache__|\.env$)" ; echo "found: $?"
   ```

   Expect no file names and `found: 1`.

## Mistakes people make

- **Committing secrets** or the `.env` file. Ignore it and commit a `.env.example` with fake values.
- **Committing generated files** (virtual environments, caches).
- **Huge commits with a message like "changes".** Small commits with a reason are readable history.
- **Rewriting history others have pulled.**
- **Assuming a tag moves.** It does not; create a new one.

## Check yourself

1. What is the difference between a branch and a tag?
   Answer: a branch moves with new commits; a tag stays on one commit.
2. What does `git archive` produce?
   Answer: the files of a commit as an archive, without the `.git/` folder.
3. Why is `.env` in `.gitignore`?
   Answer: it holds secrets and per-machine values that must not be committed.
4. How do you find out why a path is ignored?
   Answer: `git check-ignore -v <path>`.
5. What does `.editorconfig` do?
   Answer: sets indentation and line-ending rules in editors for everyone who edits the repository.

## Further reading

- Git documentation (the version here is whatever `git --version` prints; commands used are stable): https://git-scm.com/docs
- Pro Git book: https://git-scm.com/book/en/v2
- gitignore patterns: https://git-scm.com/docs/gitignore
- EditorConfig: https://editorconfig.org/
