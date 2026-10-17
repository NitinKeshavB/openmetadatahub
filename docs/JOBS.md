# Running the checks as Databricks jobs

## Runtime model
- **Engine:** the Spark session of the job task. Data quality, schema and freshness queries, `information_schema` reads and `SHOW GRANTS` all run inside Databricks. Only `SELECT/SHOW/WITH/DESCRIBE` statements are ever issued by the executor.
- **State:** append-only Delta tables in `<results_catalog>.<results_schema>`. Nothing is kept in the job.
- **Enforcement:** a task raises `EnforcementFailed` after all results are written, so the Databricks run fails, notifies, and blocks downstream tasks.

## Tasks
| Entry point | Does | Fails when (gate mode) |
|---|---|---|
| `omh-enforce-contracts` | Parses ODCS contracts, runs generated tests, validates schema, tracks contract version | An active contract has a failed or aborted test or schema issue at or above `--gate-severity`, an invalid contract file, or a non-compliant version change |
| `omh-audit-access` | Snapshots grants, masks, filters and tags per contracted table and evaluates the access policy | Findings at or above the gate severity, or the audit could not see grants (`JobConfig.fail_on_incomplete_audit`, on by default; not a CLI flag) |
| (every task) | logs JSON through `openmetadatahub.runlog` with the run id bound, and appends its events to `run_events` | — |
| `omh-assess-ai` | Builds EU AI Act / NIST AI RMF evidence for AI assets from stored quality, access and contract results | An AI asset's source data has failing, missing or stale (older than `--max-evidence-age-hours`) evidence |

Rules: missing evidence is never a pass; only `active` contracts are enforced (draft ones warn); `--enforcement report` records everything but never fails; a contract version change is judged against the last compliant version and keeps blocking until a compliant version replaces it.

## Deploy
1. Put ODCS YAML files in a Unity Catalog volume. Replace every `PLACEHOLDER-` value in `databricks.yml` (hosts, catalogs,
   service principal, budget policy, Teams destination, notification email, cost centre, region); `make check-placeholders`
   lists them and deploys refuse while any remain.
2. Create a service principal for prod and give it: `SELECT` + `USE CATALOG/SCHEMA` on contracted tables; `CREATE SCHEMA`/`CREATE TABLE` on the results catalog; `READ VOLUME` on the contracts volume; and admin, owner or `READ METADATA`/`MANAGE` on the tables for `SHOW GRANTS`.
3. `databricks bundle validate -t dev`, then `databricks bundle deploy -t dev`, then `databricks bundle run omh_governance_daily -t dev`.
4. Prod: `schedule_status` is `UNPAUSED` only in prod; keep it `PAUSED` until the first manual run is green.
5. From then on deploy through CI: merge to `main` deploys dev and test; a `v*` tag deploys uat, preprod and prod.

## Preventive vs detective
- **Preventive:** in your pipeline job add a `run_job_task` pointing at `omh_contract_gate` (parameter `contract_ids`) before the publish step; a violated contract stops the pipeline.
- **Detective:** the scheduled `omh_governance_daily` and the table-update trigger catch drift and access changes, and feed the AI gate.

## Not verified live
Serverless `SparkSession`, Delta writes to Unity Catalog, `SHOW GRANTS` semantics, the `.internal/*.whl` dependency path, `environment_version`, `table_update` trigger options. Local tests use real Spark with an emulated Unity Catalog.
