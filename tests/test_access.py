from openmetadatahub.access import AccessPolicy, evaluate, snapshot_table
from openmetadatahub.common import Severity
from openmetadatahub.contracts import load_contract

from conftest import CONTRACT_V1

TABLE = "main.sales.orders"

POLICY = AccessPolicy.from_yaml("""
admin_principals: [data_platform_admins]
role_principals: {sales_analysts: sales-analysts-grp, sales_etl: sales-etl-grp}
classification_rules:
  - tag: sensitivity
    values: [pii]
    mask_function: main.governance.mask_email
    allowed_principals: [pii-readers]
    deny_principals: [account users]
""")


def _grant(principal, action, otype, key):
    # shape documented for SHOW GRANTS: principal, ActionType, ObjectType, ObjectKey
    return {"principal": principal, "ActionType": action, "ObjectType": otype, "ObjectKey": key}


def _populate(fake, *, masked=False):
    fake.when("SHOW GRANTS ON TABLE", [
        _grant("sales-analysts-grp", "SELECT", "TABLE", TABLE),
        _grant("jo@corp.com", "SELECT", "TABLE", TABLE),
        _grant("data_platform_admins", "ALL PRIVILEGES", "TABLE", TABLE),
        _grant("account users", "SELECT", "SCHEMA", "main.sales"),  # inherited rows are listed too
    ])
    fake.when("SHOW GRANTS ON SCHEMA", [
        _grant("sales-analysts-grp", "USE", "DATABASE", "main.sales"),   # legacy spelling of USE SCHEMA
        _grant("account users", "SELECT", "SCHEMA", "main.sales"),       # duplicate of the inherited row
    ])
    fake.when("SHOW GRANTS ON CATALOG", [_grant("sales-analysts-grp", "USE CATALOG", "CATALOG", "main")])
    fake.when("column_tags", [{"column_name": "email", "tag_name": "sensitivity", "tag_value": "PII"}])
    fake.when("table_tags", [])
    fake.when("column_masks", [{"column_name": "email", "mask_catalog": "main", "mask_schema": "governance",
                                "mask_name": "mask_email", "mask_col_usage": None}] if masked else [])
    fake.when("row_filters", [{"filter_catalog": "main", "filter_schema": "governance", "filter_name": "region_rf",
                               "filter_col_usage": "region, country"}])
    fake.when("information_schema.columns", [{"column_name": "order_id"}, {"column_name": "email"}])


def test_snapshot_collects_every_source_and_dedupes(fake):
    _populate(fake, masked=True)
    snap = snapshot_table(fake, TABLE)
    levels = {(g.principal, g.privilege): g.level for g in snap.grants}
    assert levels[("sales-analysts-grp", "SELECT")] == "table"
    assert levels[("account users", "SELECT")] == "schema"
    assert levels[("sales-analysts-grp", "USE SCHEMA")] == "schema"   # normalised from USE / DATABASE
    assert levels[("sales-analysts-grp", "USE CATALOG")] == "catalog"
    assert len(snap.grants) == len(levels) == 6                       # duplicate row collapsed
    assert snap.column_masks[0].function == "main.governance.mask_email"
    assert snap.row_filters[0].function == "main.governance.region_rf"
    assert snap.row_filters[0].target_columns == ["region", "country"]
    assert snap.tags[0].column == "email" and snap.columns == ["order_id", "email"]
    assert snap.caveats == []


def test_grants_come_from_show_grants_not_information_schema(fake):
    _populate(fake)
    snapshot_table(fake, TABLE)
    assert not any("_privileges" in sql for sql in fake.log)  # those views only show the caller's own grants
    assert any(sql.startswith("SHOW GRANTS ON TABLE `main`.`sales`.`orders`") for sql in fake.log)
    # filters and masks use the documented schema_name/table_name columns
    assert any("row_filters WHERE schema_name = 'sales' AND table_name = 'orders'" in sql for sql in fake.log)
    assert any("column_masks WHERE schema_name = 'sales' AND table_name = 'orders'" in sql for sql in fake.log)


def test_unmasked_pii_flags_forbidden_and_exposed_readers(fake):
    _populate(fake, masked=False)
    findings = evaluate(snapshot_table(fake, TABLE), POLICY)
    by = {(f.code, f.principal): f for f in findings}
    exposed = by[("unmasked-sensitive-column", None)]
    assert exposed.severity == Severity.ERROR and "sales-analysts-grp" in exposed.message
    assert "SET MASK `main`.`governance`.`mask_email`" in exposed.remedy
    assert by[("forbidden-grantee", "account users")].severity == Severity.CRITICAL
    assert by[("direct-user-grant", "jo@corp.com")].remedy == "REVOKE SELECT ON TABLE `main`.`sales`.`orders` FROM `jo@corp.com`;"
    assert not any(f.principal == "data_platform_admins" for f in findings)  # admins exempt


def test_masking_clears_the_sensitive_column_findings(fake):
    _populate(fake, masked=True)
    codes = {f.code for f in evaluate(snapshot_table(fake, TABLE), POLICY)}
    assert "unmasked-sensitive-column" not in codes and "forbidden-grantee" not in codes


def test_contract_roles_drive_missing_and_unexpected_grants(fake):
    _populate(fake, masked=True)
    findings = evaluate(snapshot_table(fake, TABLE), POLICY, load_contract(CONTRACT_V1))
    by = {(f.code, f.principal): f for f in findings}
    missing = {f.remedy for f in findings if f.code == "missing-grant" and f.principal == "sales-etl-grp"}
    assert missing == {
        "GRANT SELECT ON TABLE `main`.`sales`.`orders` TO `sales-etl-grp`;",
        "GRANT MODIFY ON TABLE `main`.`sales`.`orders` TO `sales-etl-grp`;",
    }
    # analysts hold SELECT + USE CATALOG + USE SCHEMA, so nothing is missing for them
    assert not [f for f in findings if f.principal == "sales-analysts-grp" and f.code.startswith("missing")]
    # 'account users' reads through the schema but no contract role maps to it; no table-level REVOKE can fix that
    unexpected = by[("unexpected-grant", "account users")]
    assert "inherited from SCHEMA" in unexpected.message and unexpected.remedy is None
    assert by[("unexpected-grant", "jo@corp.com")].remedy.startswith("REVOKE")


def test_missing_usage_privileges_are_called_out(fake):
    _populate(fake, masked=True)
    fake.rules = [r for r in fake.rules if r[0] not in ("SHOW GRANTS ON SCHEMA", "SHOW GRANTS ON CATALOG")]
    fake.when("SHOW GRANTS ON SCHEMA", []).when("SHOW GRANTS ON CATALOG", [])
    findings = evaluate(snapshot_table(fake, TABLE), POLICY, load_contract(CONTRACT_V1))
    usage = [f for f in findings if f.code == "missing-usage" and f.principal == "sales-analysts-grp"]
    assert {f.remedy.split(" ON ")[0] for f in usage} == {"GRANT USE CATALOG", "GRANT USE SCHEMA"}


def test_invisible_grants_are_reported_as_a_caveat_not_as_clean(fake):
    snap = snapshot_table(fake, TABLE)  # nothing visible at all
    assert any("no grants visible" in c for c in snap.caveats)
    assert any(f.code == "snapshot-caveat" for f in evaluate(snap, POLICY))


def test_insufficient_privilege_on_show_grants_is_a_caveat(fake):
    _populate(fake)
    fake.rules.insert(0, ("SHOW GRANTS ON CATALOG", PermissionError("PERMISSION_DENIED")))
    snap = snapshot_table(fake, TABLE)
    assert any("grants (catalog) unavailable" in c for c in snap.caveats)
    assert snap.grants  # table and schema grants still loaded


def test_failing_information_schema_query_becomes_a_caveat(fake):
    _populate(fake)
    fake.rules.insert(0, ("row_filters", RuntimeError("no such relation")))
    snap = snapshot_table(fake, TABLE)
    assert any("row_filters unavailable" in c for c in snap.caveats)
    assert snap.grants
