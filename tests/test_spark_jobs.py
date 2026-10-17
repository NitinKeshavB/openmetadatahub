"""The Databricks jobs end to end on a real Spark engine: results land in tables, gates raise, state persists."""

import itertools
import json

import pytest

pytest.importorskip("pyspark")

from openmetadatahub.jobs import (ConfigError, EnforcementFailed, JobConfig, ResultStore, assess_ai, audit_access,  # noqa: E402
                                  enforce_contracts)
from openmetadatahub.jobs.entrypoints import enforce_contracts_main  # noqa: E402

from conftest import CONTRACT_V1  # noqa: E402
from spark_support import CAT, LocalExecutor, refresh_information_schema, set_rows  # noqa: E402

CONTRACT = CONTRACT_V1.replace("main.sales.orders", f"{CAT}.shop.orders")
_n = itertools.count()


def make_orders(spark, *, healthy: bool):
    spark.sql("CREATE DATABASE IF NOT EXISTS shop")
    spark.sql("DROP TABLE IF EXISTS shop.orders")
    age = "1 HOURS" if healthy else "48 HOURS"
    rows = ("(1,'a@x.com','NEW',5.0),(2,'b@x.com','PAID',6.0),(3,'c@x.com','SHIPPED',7.0)" if healthy else
            "(1,'a@x.com','NEW',5.0),(1,'bad-email','WEIRD',-6.0),(3,NULL,NULL,7.0)")
    spark.sql(f"CREATE TABLE shop.orders AS SELECT *, current_timestamp() - INTERVAL {age} AS updated_at "
              f"FROM VALUES {rows} AS t(order_id, email, status, amount)")
    refresh_information_schema(spark)


@pytest.fixture
def env(spark, tmp_path):
    """A fresh contracts folder and a fresh results schema per test, so runs never leak into each other."""
    folder = tmp_path / "contracts"
    folder.mkdir()
    (folder / "orders.yaml").write_text(CONTRACT)
    schema = f"governance_{next(_n)}"

    def cfg(**kw):
        return JobConfig(contracts_path=str(folder), results_catalog=CAT, results_schema=schema, **kw)

    def table(name):
        return spark.table(f"{CAT}.{schema}.{name}")

    return type("Env", (), {"folder": folder, "cfg": staticmethod(cfg), "table": staticmethod(table), "spark": spark,
                            "executor": LocalExecutor(spark)})


def test_healthy_data_passes_and_everything_is_recorded(env):
    make_orders(env.spark, healthy=True)
    out = enforce_contracts(env.spark, env.cfg())
    assert out.decisions == {"orders-contract": "pass"}

    run = env.table("contract_runs").collect()[0]
    assert (run.decision, run.blocking, run.tests_total, run.tests_failed, run.tests_aborted) == ("pass", 0, 8, 0, 0)
    assert env.table("dq_results").count() == 8
    score = env.table("quality_scores").collect()[0]
    assert score.table_name == f"{CAT}.shop.orders" and score.score == 100.0
    assert json.loads(score.by_dimension)["timeliness"] == 100.0
    assert env.table("contract_versions").collect()[0].diff_summary == "first version seen"
    # warnings are recorded but do not block (required columns are nullable in Spark; amount is undeclared)
    codes = {r.code for r in env.table("schema_issues").collect()}
    assert {"nullable-but-required", "undeclared-column"} <= codes


def test_violations_close_the_gate_but_everything_is_still_recorded(env):
    make_orders(env.spark, healthy=False)
    with pytest.raises(EnforcementFailed, match="orders-contract"):
        enforce_contracts(env.spark, env.cfg())

    run = env.table("contract_runs").collect()[0]
    assert run.decision == "blocked" and run.blocking >= 4
    failed = {r.test_name for r in env.table("dq_results").where("status = 'failed'").collect()}
    assert "orders.order_id:unique" in failed            # duplicate id
    assert "orders.email:invalidValues" in failed        # 'bad-email'
    assert "orders.updated_at:sla-freshness" in failed   # 48h old against a 24h SLA
    assert "orders:sql0" in failed or any(n.endswith(":custom_sql") or "sql" in n.lower() for n in failed)  # amount < 0
    assert env.table("quality_scores").collect()[0].score < 100


def test_report_mode_records_without_failing_the_task(env):
    make_orders(env.spark, healthy=False)
    out = enforce_contracts(env.spark, env.cfg(enforcement="report"))
    assert out.blocked == ["orders-contract"]
    assert env.table("contract_runs").collect()[0].decision == "blocked"


def test_draft_contracts_are_checked_but_never_block(env):
    (env.folder / "orders.yaml").write_text(CONTRACT.replace("status: active", "status: draft"))
    make_orders(env.spark, healthy=False)
    out = enforce_contracts(env.spark, env.cfg())  # gate mode, no exception
    assert out.decisions == {"orders-contract": "warn"}


def test_missing_table_is_a_blocking_schema_issue(env):
    env.spark.sql("DROP TABLE IF EXISTS shop.orders")
    refresh_information_schema(env.spark)
    with pytest.raises(EnforcementFailed):
        enforce_contracts(env.spark, env.cfg())
    assert {r.code for r in env.table("schema_issues").collect()} == {"table-missing"}
    assert env.table("contract_runs").collect()[0].tests_aborted > 0  # the tests could not run, and did not pass


def test_unreadable_evidence_never_counts_as_a_pass(env, tmp_path):
    (env.folder / "broken.yaml").write_text("id: [unclosed")
    make_orders(env.spark, healthy=True)
    with pytest.raises(EnforcementFailed, match="broken.yaml"):
        enforce_contracts(env.spark, env.cfg())
    runs = {r.contract_id: r.decision for r in env.table("contract_runs").collect()}
    assert runs == {"broken.yaml": "invalid", "orders-contract": "pass"}  # the good contract was still checked

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConfigError, match="no contracts found"):
        enforce_contracts(env.spark, JobConfig(contracts_path=str(empty), results_catalog=CAT, results_schema="governance_x"))
    (env.folder / "broken.yaml").unlink()  # an unparseable file could be the requested id, so it blocks instead
    with pytest.raises(ConfigError, match="nope"):
        enforce_contracts(env.spark, env.cfg(contract_ids=("nope",)))
    with pytest.raises(ConfigError, match="path not found"):
        enforce_contracts(env.spark, JobConfig(contracts_path="/does/not/exist", results_catalog=CAT, results_schema="governance_x"))


def test_a_breaking_change_without_a_major_bump_blocks_until_a_compliant_version_replaces_it(env):
    make_orders(env.spark, healthy=True)
    assert enforce_contracts(env.spark, env.cfg()).decisions["orders-contract"] == "pass"

    breaking = CONTRACT.replace("      - name: updated_at\n        logicalType: timestamp\n", "")
    (env.folder / "orders.yaml").write_text(breaking)          # column removed, still version 1.0.0
    with pytest.raises(EnforcementFailed):
        enforce_contracts(env.spark, env.cfg())
    note = env.table("contract_versions").where("NOT version_ok").collect()[0].diff_summary
    assert "required major bump, declared none" in note and "column-removed:orders.updated_at" in note

    with pytest.raises(EnforcementFailed):                      # nothing changed: the verdict persists, no free pass
        enforce_contracts(env.spark, env.cfg())
    assert env.table("contract_versions").count() == 2          # no duplicate row for an unchanged file

    (env.folder / "orders.yaml").write_text(breaking.replace("version: 1.0.0", "version: 1.0.1"))
    with pytest.raises(EnforcementFailed):                      # a patch bump does not legitimise a breaking change
        enforce_contracts(env.spark, env.cfg())

    (env.folder / "orders.yaml").write_text(breaking.replace("version: 1.0.0", "version: 2.0.0"))
    assert enforce_contracts(env.spark, env.cfg()).decisions["orders-contract"] == "pass"


def test_entry_point_runs_the_same_job_from_cli_arguments(env, capsys):
    make_orders(env.spark, healthy=True)
    args = ["--contracts-path", str(env.folder), "--results-catalog", CAT, "--results-schema", env.cfg().results_schema,
            "--run-id", "job-run-42", "--contract-ids", "orders-contract"]
    enforce_contracts_main(args)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["record"]
    assert line["message"] == "[contracts] pass orders-contract" and line["extra"]["run_id"] == "job-run-42"
    assert env.table("contract_runs").collect()[0].run_id == "job-run-42"
    events = env.table("run_events").orderBy("seq").collect()
    assert [(e.run_id, e.level, e.message) for e in events] == [("job-run-42", "INFO", "[contracts] pass orders-contract")]

    make_orders(env.spark, healthy=False)
    with pytest.raises(EnforcementFailed):
        enforce_contracts_main(args)
    failed = env.table("run_events").where("level = 'ERROR'").collect()
    assert len(failed) == 1 and "omh-enforce-contracts failed" in failed[0].message  # recorded even though it raised


# ------------------------------------------------------------------ access

POLICY = """
admin_principals: [data_platform_admins]
role_principals: {sales_analysts: analysts-grp, sales_etl: etl-grp}
classification_rules:
  - tag: sensitivity
    values: [pii]
    mask_function: gov.fn.mask_email
    deny_principals: [account users]
"""


def _g(p, a, t, k):
    return {"principal": p, "ActionType": a, "ObjectType": t, "ObjectKey": k}


COMPLIANT_GRANTS = {
    "TABLE": [_g("analysts-grp", "SELECT", "TABLE", "t"), _g("etl-grp", "SELECT", "TABLE", "t"), _g("etl-grp", "MODIFY", "TABLE", "t")],
    "SCHEMA": [_g("analysts-grp", "USE SCHEMA", "SCHEMA", "s"), _g("etl-grp", "USE SCHEMA", "SCHEMA", "s")],
    "CATALOG": [_g("analysts-grp", "USE CATALOG", "CATALOG", "c"), _g("etl-grp", "USE CATALOG", "CATALOG", "c")],
}


def _tag_and_mask(spark, masked: bool):
    set_rows(spark, "column_tags", [("shop", "orders", "email", "sensitivity", "PII")])
    set_rows(spark, "column_masks", [("shop", "orders", "email", CAT, "gov", "mask_email", None)] if masked else [])


def test_access_audit_blocks_unmasked_pii_and_passes_when_fixed(env, tmp_path):
    make_orders(env.spark, healthy=True)
    policy = tmp_path / "policy.yaml"
    policy.write_text(POLICY)
    cfg = env.cfg(access_policy_path=str(policy))
    table = f"{CAT}.shop.orders"

    grants = {**COMPLIANT_GRANTS, "TABLE": COMPLIANT_GRANTS["TABLE"] + [_g("account users", "SELECT", "TABLE", "t")]}
    _tag_and_mask(env.spark, masked=False)
    with pytest.raises(EnforcementFailed, match=table):
        audit_access(env.spark, cfg, LocalExecutor(env.spark, grants))
    codes = {r.code: r.severity for r in env.table("access_findings").collect()}
    assert codes["forbidden-grantee"] == "critical" and "unmasked-sensitive-column" in codes
    remedy = env.table("access_findings").where("code = 'forbidden-grantee'").collect()[0].remedy
    assert remedy.startswith("REVOKE SELECT ON TABLE")                      # advice only: nothing was executed
    assert env.spark.sql("SELECT COUNT(*) c FROM shop.orders").collect()[0].c == 3

    _tag_and_mask(env.spark, masked=True)
    out = audit_access(env.spark, cfg, LocalExecutor(env.spark, COMPLIANT_GRANTS))
    assert out.decisions == {table: "pass"}
    assert env.table("access_audits").where("blocking = 0").count() == 1


def test_an_audit_that_cannot_see_grants_is_not_a_pass(env, tmp_path):
    make_orders(env.spark, healthy=True)
    _tag_and_mask(env.spark, masked=True)
    policy = tmp_path / "policy.yaml"
    policy.write_text(POLICY)
    env.cfg = lambda cfg=env.cfg, **kw: cfg(access_policy_path=str(policy), **kw)
    blind = {"TABLE": PermissionError("PERMISSION_DENIED"), "SCHEMA": [], "CATALOG": []}
    with pytest.raises(EnforcementFailed):
        audit_access(env.spark, env.cfg(), LocalExecutor(env.spark, blind))
    assert "unavailable" in env.table("access_audits").collect()[0].caveats
    # Blind, the audit also sees every required role grant as missing, so it blocks even with the caveat rule off.
    with pytest.raises(EnforcementFailed):
        audit_access(env.spark, env.cfg(fail_on_incomplete_audit=False), LocalExecutor(env.spark, blind))
    codes = {r.code for r in env.table("access_findings").collect()}
    assert {"snapshot-caveat", "missing-grant"} <= codes


# ------------------------------------------------------------------ AI

MANIFESTS = f"""
apiVersion: v1
kind: List
items:
  - kind: InferenceService
    metadata:
      name: churn
      namespace: prod
      annotations:
        openmetadatahub.io/owner: ml-team
        openmetadatahub.io/approved-by: cdo
        openmetadatahub.io/risk-class: high
        openmetadatahub.io/stage: production
        openmetadatahub.io/intended-use: churn scoring
        openmetadatahub.io/limitations: consumer accounts only
        openmetadatahub.io/human-oversight: agent reviews each offer
        openmetadatahub.io/data-sources: {CAT}.shop.orders
  - kind: TrustyAIService
    metadata: {{name: tas, namespace: prod}}
"""


def test_ai_gate_uses_the_stored_quality_and_access_evidence(env, tmp_path):
    make_orders(env.spark, healthy=True)
    _tag_and_mask(env.spark, masked=True)
    manifests = tmp_path / "ai"
    manifests.mkdir()
    (manifests / "odh.yaml").write_text(MANIFESTS)
    policy = tmp_path / "policy.yaml"
    policy.write_text(POLICY)
    cfg = env.cfg(ai_manifests_path=str(manifests), access_policy_path=str(policy))

    # No evidence has been produced yet: the model is blocked because its source data is unverified.
    with pytest.raises(EnforcementFailed, match="odh:prod/churn"):
        assess_ai(env.spark, cfg)
    checks = {c["code"]: c for c in json.loads(env.table("ai_assessments").collect()[0].checks)}
    assert checks[f"quality:{CAT}.shop.orders"]["message"] == "no data quality result for this source"

    enforce_contracts(env.spark, cfg)
    audit_access(env.spark, cfg, LocalExecutor(env.spark, COMPLIANT_GRANTS))
    out = assess_ai(env.spark, cfg)
    assert out.decisions == {"odh:prod/churn": "pass"}
    latest = env.table("ai_assessments").orderBy("run_ts").collect()[-1]
    assert latest.blocked is False and json.loads(latest.control_coverage)["EUAIA-10"]["status"] == "pass"

    # Corrupt the data and re-run enforcement in report mode: the *model* is now blocked by its data.
    make_orders(env.spark, healthy=False)
    enforce_contracts(env.spark, env.cfg(enforcement="report"))
    with pytest.raises(EnforcementFailed, match="odh:prod/churn"):
        assess_ai(env.spark, cfg)
    assert any(c["status"] == "fail" and c["code"].startswith("quality:")
               for c in json.loads(env.table("ai_assessments").orderBy("run_ts").collect()[-1].checks))


def test_stale_evidence_counts_as_missing(env, tmp_path):
    make_orders(env.spark, healthy=True)
    enforce_contracts(env.spark, env.cfg())
    manifests = tmp_path / "m.yaml"
    manifests.write_text(MANIFESTS)
    with pytest.raises(EnforcementFailed):
        assess_ai(env.spark, env.cfg(ai_manifests_path=str(manifests), max_evidence_age_hours=0))
    assert ResultStore(env.spark, CAT, env.cfg().results_schema).latest_quality_scores(0) == {}
    assert list(ResultStore(env.spark, CAT, env.cfg().results_schema).latest_quality_scores(48)) == [f"{CAT}.shop.orders"]
