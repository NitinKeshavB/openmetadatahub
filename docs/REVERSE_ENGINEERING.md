# Reverse engineering OpenMetadata and Open Data Hub

What was read, what was found, and how it shaped `openmetadatahub`.

## Method and provenance

| Target | Commit | Date | How it was read |
|---|---|---|---|
| open-metadata/OpenMetadata | `3b27f40` | 2026-10-11 | Sparse checkout: spec JSON schemas, ingestion data quality and Databricks connector, server security package |
| opendatahub-io/opendatahub-operator | `62f9b93` | 2026-10-09 | API types and component list |
| opendatahub-io/odh-dashboard | `7f54754` | 2026-10-10 | Package list, plus **REA** static analysis of `frontend/` |
| trustyai-explainability/trustyai-service-operator | `359bc8e` | 2026-10-09 | `ARCHITECTURE.md`, CRD types |
| morluto/rea | `a00c34d` | 2026-10-11 | The reverse-engineering tool itself |

**What REA did.** `analyze-javascript-application` ran over `odh-dashboard/frontend`: 2,621 source files, 2,614 parsed, 0 parse failures, a graph of 10,912 nodes and 11,724 edges (3,895 recorded limitations, i.e. things it could not resolve statically). It showed the dashboard is a module-federation host, with `modelRegistry` and `evalHub` loaded as federated remotes, and uses `@openshift/dynamic-plugin-sdk`.

**What REA did not do.** REA is built for binaries, Electron apps, minified bundles and sites where source is unavailable. Both targets here are open source, so reading the source directly was more precise than reverse engineering, and most findings below come from that. REA was only run on the dashboard `frontend/` directory, not on `packages/` (where the TrustyAI and model-registry UIs live), so the federation findings cover the host app only.

## Licensing finding (read before reusing any code)

OpenMetadata is not uniformly Apache-2.0. Per its own `AGENTS.md` and `ingestion/LICENSE`:

- Server, spec, UI: Apache-2.0.
- `ingestion/` (Python, including every data quality validator and the Databricks connector): **Collate Community License 1.0**, which grants use and modification but bars using it for a service that competes with Collate's.

`openmetadatahub` is therefore an original implementation of the same *ideas*. No ingestion code was copied. Open Data Hub repos read here are Apache-2.0.

## OpenMetadata: what it does

**Data quality** (`openmetadata-spec/.../tests/`, `ingestion/.../data_quality/`)
- Three-part model: TestDefinition, TestCase, TestSuite (executable per-table, or logical to group cases across tables).
- Eight dimensions: Completeness, Accuracy, Consistency, Validity, Uniqueness, Integrity, SQL, NoDimension.
- 27 SQLAlchemy validators: 10 table-level (row count, column count, column names, custom SQL, table diff, rows inserted, ...) and 17 column-level (not null, unique, in/not-in set, regex, between, length, min/max/mean/median/stddev/sum, missing count, ...).
- Dimension-level results with an impact score, plus incident handling (`testCaseResolutionStatus`).

**Data contracts** (`entity/data/dataContract.json`, `entity/datacontract/`)
- Fields: schema, semantics rules, terms of use, security (classification, access policy, identities, row filters), SLA (refresh frequency, max latency, availability, retention), quality expectations, ODCS round-trip (`odcsQualityRules`).
- Validation results are split into schema, SLA, quality, semantics.

**AI governance** (`entity/ai/`)
- `aiApplication`, `llmModel`, `mcpServer`, `promptTemplate`, `agentExecution`, `aiGovernancePolicy`, `aiGovernanceFramework`, `aiFrameworkControl`, `auditReport`.
- Policies cover model approval, data access, bias thresholds, compliance checks, cost control and performance standards. Frameworks are control libraries with an assessment cadence.

**Access control** (`openmetadata-service/.../security/policyevaluator/`)
- Rule-based RBAC over its own entities (who can edit a table's description in OpenMetadata). It does not manage permissions inside the source system.

**Databricks connector** (`ingestion/.../source/databricks/`)
- Reads: catalogs, schemas, tables, native tags (`information_schema.*_tags`), table and column lineage (`system.access.table_lineage` / `column_lineage`), query history, SCIM groups and service principals for owner resolution, jobs and pipelines.
- **Gap, confirmed by search:** `grep -i "grant|row_filter|column_mask|privilege"` over the connector returns only one error-message string. It does not read grants, row filters or column masks. A contract can *declare* an access policy, but nothing compares it to what Databricks actually enforces.

## Open Data Hub: what it does

- **Operator** (`api/components/v1alpha1`): a DataScienceCluster composes components: kserve, modelregistry, trustyai, feastoperator, mlflowoperator, llamastackoperator, mcplifecycleoperator, aigateway, aihub, workbenches, pipelines, ray, kueue, trainer and others.
- **TrustyAI operator**: one binary, four services selected by `--enable-services`: TrustyAIService (explainability, fairness, drift), LMEvalJob (LLM evaluation), EvalHub, NemoGuardrails. CRDs live in `trustyai.opendatahub.io`.
- **Dashboard**: federated micro-frontends (per REA), 37 packages including trustyai, model-registry, eval-hub, feature-store, data-connect-hub, observability.
- **No table governance.** A repo-wide search of the operator API, TrustyAI and dashboard for data contracts, data quality tools or Unity Catalog found nothing relevant. Open Data Hub governs *models* (serving, monitoring, evaluation, guardrails), not the data they depend on.

## What this means for `openmetadatahub`

| Capability | OpenMetadata | Open Data Hub | `openmetadatahub` |
|---|---|---|---|
| Data quality tests | Yes (27 validators) | No | 18 metrics compiled to Databricks SQL, severity-weighted score |
| Data contracts | Yes (ODCS import/export) | No | ODCS v3.x subset: contract to tests, schema check against Unity Catalog, breaking-change and version-bump check |
| Databricks grants, masks, filters | Not read | No | Read via `SHOW GRANTS` and `information_schema`, compared to contract roles and tag-driven policy |
| AI asset inventory | Entities, manual registration | CRDs, no governance record | Discovers assets from ODH manifests; unannotated ones surface as UNAUTHORIZED |
| AI governance gate | Policy entities | Monitoring and evaluation only | Blocks an AI asset when its *source tables* fail quality, contract or access checks |

The deliberate bridge is the last row: neither project connects "is this model governed?" to "is the data it learns from governed?". `ai/assess.py` does.

## Databricks facts verified against the docs

The first draft of the access module was wrong and was corrected after checking the Databricks documentation:

- `information_schema.*_privileges` shows only the **caller's own** grants, even for admins. The complete picture comes from `SHOW GRANTS ON ...` (principal, actionType, objectType, objectKey), which needs admin/owner or READ METADATA/MANAGE plus usage on parents. The module now uses `SHOW GRANTS`.
- `row_filters` and `column_masks` use `schema_name` / `table_name` (not `table_schema`) and `filter_col_usage` / `mask_col_usage`.
- `SHOW GRANTS` does not expose WITH GRANT OPTION, so the grant-option check was removed rather than left as a check that can never fire.

## What is not verified

- **Nothing has run against a live Databricks workspace.** Tests use a fake executor. SQL syntax is parsed with `sqlglot` (Databricks dialect), which proves it is well-formed, not that it behaves correctly on your data.
- `DatabricksSqlExecutor` has never connected to a warehouse.
- The `SHOW GRANTS` output shape comes from the docs; the legacy spelling (`USE` on `DATABASE`) is handled defensively but untested against real output.
- EU AI Act and NIST references are coarse control mappings. A passing check is evidence toward a control, not a compliance determination.
- ODCS support is a subset; `text` and `custom` quality rules are ignored rather than executed.
