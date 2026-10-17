# openmetadatahub

Data quality, data contracts, Databricks access governance and AI governance in one small, testable library with an HTTP API and CLI.

It is an original implementation informed by a reverse-engineering study of [OpenMetadata](https://github.com/open-metadata/OpenMetadata) and [Open Data Hub](https://github.com/opendatahub-io). See [docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md) for findings, evidence and the licensing caveat.

## What it does

| Module | Does |
|---|---|
| `dq` | 18 metrics (completeness, uniqueness, validity, accuracy, integrity, timeliness, volume, custom SQL) compiled to Databricks SQL; severity-weighted quality score by dimension. Checks that cannot run count as failures. |
| `contracts` | Loads ODCS v3.x contracts; compiles them to tests; checks declared schema against Unity Catalog; detects breaking changes and whether the version bump is big enough. |
| `access` | Snapshots grants (`SHOW GRANTS`), row filters, column masks and tags for a table; flags unmasked sensitive columns, forbidden grantees, missing or unexpected grants against contract roles. Remediation is dry-run SQL only. |
| `ai` | Inventories AI assets from Open Data Hub manifests; assesses them against policy using the quality, contract and access state of their **source tables**; rolls results up to EU AI Act / NIST AI RMF controls. |

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest

openmetadatahub metrics
openmetadatahub contract-tests examples/orders.contract.yaml
openmetadatahub contract-diff old.yaml new.yaml     # exit 1 if a breaking change lacks a major bump
openmetadatahub ai-discover examples/odh-manifests.yaml
openmetadatahub serve
```

Programmatic use against a warehouse (`pip install -e ".[databricks]"`):

```python
from openmetadatahub.common import DatabricksSqlExecutor
from openmetadatahub.contracts import load_contract, contract_to_tests, validate_schema
from openmetadatahub.dq import run_cases, summarise

ex = DatabricksSqlExecutor(server_hostname, http_path, token)   # use a read-only service principal
contract = load_contract("examples/orders.contract.yaml")
report = summarise(contract.id, run_cases(contract_to_tests(contract), ex))
issues = validate_schema(contract, ex)
```

```python
from openmetadatahub.access import AccessPolicy, snapshot_table, evaluate
snap = snapshot_table(ex, "main.sales.orders")
findings = evaluate(snap, AccessPolicy.from_yaml(open("examples/access-policy.yaml").read()), contract)
```

The AI gate ties it together: pass each asset's table scores and access findings into `ai.Evidence` and call `ai.assess(asset, policy, evidence)`.

## How the jobs run (Databricks is the engine)

The checks run as Databricks Jobs on serverless compute. Each task is a wheel entry point (`omh-enforce-contracts`, `omh-audit-access`, `omh-assess-ai`) that uses the job's own Spark session, so every generated query executes inside Databricks and no token or SQL warehouse is needed. Results are appended to Delta tables in `<results_catalog>.<results_schema>` (`contract_runs`, `dq_results`, `quality_scores`, `schema_issues`, `contract_versions`, `access_audits`, `access_findings`, `ai_assessments`). Enforcement is "record first, fail last": every contract is checked and written, then the task fails if any active contract is violated. Details and deployment steps: [docs/JOBS.md](docs/JOBS.md).

`databricks.yml` (targets `dev`, `test`, `uat`, `preprod`, `prod`) and `resources/jobs.yml` define three jobs, each tagged with its project, repository and commit, attached to a budget policy, and notifying by email and Microsoft Teams on failure: `omh_governance_daily` (scheduled; contracts and access in parallel, then the AI gate), `omh_contract_gate` (call it from a pipeline with `run_job_task` to stop downstream tasks) and `omh_orders_on_update` (example table-update trigger, paused).

## Development process

The same process as repositories generated from [cookiecutter-dbxops](https://github.com/NitinKeshavB/cookiecutter-dbxops):

```bash
make install            # editable install with test, lint and release tools
make lint               # pre-commit: black, isort, flake8, secrets, no commits to main
make test               # pytest with a 90% coverage gate
make check-placeholders # values still to fill in; STRICT=1 (used by every deploy) fails while any remain
```

CI (`.github/workflows/ci.yml`) runs lint and tests on every pull request. CD deploys `main` to dev and test, and a `v*`
tag to uat, preprod and prod behind GitHub environment reviewers, authenticating with GitHub OIDC. `version.txt` is the
only version. Jobs log JSON through `openmetadatahub.runlog` (loguru) and keep their events in the `run_events` table.
`CLAUDE.md` and `.claude/agents/code-reviewer.md` hold the rules for AI-assisted changes.

## Safety properties

- The only warehouse boundary is `SqlExecutor.query`. Nothing here writes to Databricks; remediation is returned as text.
- Identifiers are backtick-quoted and validated; values go through one literal escaper; free-form filters and custom SQL are guarded (single statement, no comments, write keywords rejected). Run it with a read-only principal regardless; the guard is a backstop, not a sandbox.
- Missing evidence is never a pass: aborted tests, absent quality scores and unevaluated thresholds all fail, and an incomplete grant listing is reported as a caveat.

## Status

90 tests pass, including metric and job tests on a real local Spark engine (pyspark 4) with hand-computed expectations. **None run against a live Databricks workspace.** Not verified live: serverless `SparkSession` use, Delta writes to Unity Catalog, `SHOW GRANTS` semantics (canned in tests), the bundle (the CLI was unavailable; the job definitions were only schema-checked with `databricks-bundles`), the wheel path `${workspace.artifact_path}/.internal/*.whl`, `environment_version`, and the `table_update` trigger. Run `databricks bundle validate` and one manual run in dev first. Required privileges for the run-as principal: `SELECT` on target tables and `USE` on their parents; `CREATE SCHEMA`/`CREATE TABLE` in the results catalog; admin, owner, or `READ METADATA`/`MANAGE` on tables for `SHOW GRANTS`, otherwise the access audit reports itself incomplete and blocks.

## Name and licence

Apache-2.0. The name combines two existing project names, OpenMetadata and Open Data Hub; check trademark guidance for both before publishing it outside your organisation.
