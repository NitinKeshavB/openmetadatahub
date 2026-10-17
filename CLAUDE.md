# CLAUDE.md — openmetadatahub

**Authority:** SYSTEM > this file > code. This repository follows the delivery process of the
[cookiecutter-dbxops](https://github.com/NitinKeshavB/cookiecutter-dbxops) master template; where this file is silent,
that template's doctrine (`{{cookiecutter.repo_name}}/CLAUDE.md`) applies.

## Hard rules

1. **Read before edit.** Read a file this turn before changing it; quote `file:line` when you cite code.
2. **Surgical changes.** Every changed line traces to the request. No drive-by reformatting, renames or refactors.
3. **Verify by running.** A change is done when `make lint` and `make test` pass and you paste the result.
   For a bug, write the failing test first.
4. **Never weaken a gate.** No deleted or loosened tests, no new `# noqa`/ignores, never lower
   `MINIMUM_TEST_COVERAGE_PERCENT` (90, in `run.sh`).
5. **Read-only by design.** Nothing in `src/` writes to a governed table or grants access; remediation is returned as
   SQL text. Keep it that way. Only `ResultStore` writes, and only to the results schema.
6. **Missing evidence is never a pass.** Aborted tests, absent scores, incomplete grant listings all fail.
7. **No non-dev deploy without approval.** test/uat/preprod/prod deploy only through `cd.yml`; deploys refuse while
   any `PLACEHOLDER-` remains (`make check-placeholders STRICT=1`).
8. **Shared state needs consent:** `.github/workflows/`, `version.txt`, tags, releases.

## Delivery process (same as cookiecutter-dbxops)

| Step | How |
|---|---|
| Branch | work on a branch; `pre-commit` blocks commits to `main` |
| Checks | `make lint` (black, isort, flake8, secrets) and `make test` (pytest + coverage gate) in CI (`ci.yml`) |
| Targets | `dev`, `test`, `uat`, `preprod`, `prod` in `databricks.yml`; non-dev run as the service principal |
| Deploy | merge to `main` → dev, test; tag `v*` → uat, preprod, prod behind GitHub environment reviewers; GitHub OIDC |
| Version | `version.txt` is the only source; `__version__` is read from package metadata |
| Cost | every job carries `project_id`, `workload`, `bundle`, `target`, `git_repo`, `git_ref`, `git_sha`, `cost_center`, `region` and the budget policy |
| Notify | every job emails `notify_email` and posts to the Teams destination on failure and on duration warnings |
| Logs | `openmetadatahub.runlog` (loguru): JSON with run context; each job's events land in `run_events` |

## Commands

`make install` · `make lint` · `make test` · `make build` · `make check-placeholders [STRICT=1]` ·
`make validate-bundle TARGET=dev`

## Review

Before asking for approval, run the `code-reviewer` agent (`.claude/agents/code-reviewer.md`) on the diff and
address its findings. Close with `In one sentence: <what was delivered>.`
