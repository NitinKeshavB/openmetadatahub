import pytest

from openmetadatahub.common import Severity
from openmetadatahub.contracts import contract_to_tests, diff_contracts, load_contract, validate_schema

from conftest import CONTRACT_V1


def test_contract_compiles_to_expected_tests():
    c = load_contract(CONTRACT_V1)
    tests = {t.name: t for t in contract_to_tests(c)}
    assert tests["orders.order_id:required"].metric == "column_not_null"
    assert tests["orders.order_id:unique"].metric == "column_unique"  # primaryKey implies both
    assert tests["orders.status:required"].metric == "column_not_null"

    pattern = tests["orders.email:invalidValues"]
    assert pattern.metric == "column_matches_regex" and pattern.max_failed_rows == 0

    valid = tests["orders.status:invalidValues"]
    assert valid.metric == "column_in_set" and valid.max_failed_pct == 1.0
    assert valid.severity == Severity.WARNING

    assert tests["orders:rowCount"].params == {"gt": 0}
    sql_rule = next(t for t in tests.values() if t.metric == "custom_sql")
    assert sql_rule.params["equals"] == 0

    fresh = tests["orders.updated_at:sla-freshness"]
    assert fresh.metric == "freshness_hours" and fresh.params["max_age_hours"] == 24
    assert all(t.source == "orders-contract" for t in tests.values())


def test_unqualified_table_needs_a_namespace():
    c = load_contract(CONTRACT_V1.replace("physicalName: main.sales.orders", "physicalName: orders"))
    with pytest.raises(ValueError, match="namespace"):
        contract_to_tests(c)
    assert contract_to_tests(c, namespace="main.sales")[0].table == "main.sales.orders"


def test_sla_unit_conversion():
    c = load_contract(CONTRACT_V1.replace("value: 24\n    unit: h", "value: 2\n    unit: d"))
    fresh = next(t for t in contract_to_tests(c) if t.metric == "freshness_hours")
    assert fresh.params["max_age_hours"] == 48


def _cols(*rows):
    return [{"column_name": n, "data_type": t, "is_nullable": nul} for n, t, nul in rows]


def test_schema_validation_reports_each_kind_of_drift(fake):
    fake.when("information_schema.columns", _cols(
        ("order_id", "BIGINT", "NO"),
        ("email", "INT", "YES"),          # wrong type
        ("status", "STRING", "YES"),      # required but nullable
        ("amount", "DECIMAL", "YES"),     # undeclared
    ))                                    # updated_at missing
    issues = {(i.column, i.code): i for i in validate_schema(load_contract(CONTRACT_V1), fake)}
    assert ("updated_at", "column-missing") in issues
    assert issues[("updated_at", "column-missing")].severity == Severity.CRITICAL
    assert ("email", "type-mismatch") in issues
    assert ("status", "nullable-but-required") in issues
    assert ("amount", "undeclared-column") in issues
    assert ("order_id", "type-mismatch") not in issues  # BIGINT is an integer


def test_missing_table_is_critical(fake):
    issues = validate_schema(load_contract(CONTRACT_V1), fake)
    assert [i.code for i in issues] == ["table-missing"]


def _bump(version, edit=None):
    text = CONTRACT_V1.replace("version: 1.0.0", f"version: {version}")
    return load_contract(edit(text) if edit else text)


def test_breaking_change_requires_major_bump():
    old = _bump("1.0.0")
    new = _bump("1.1.0", lambda t: t.replace("      - name: updated_at\n        logicalType: timestamp\n", ""))
    d = diff_contracts(old, new)
    assert any(c.code == "column-removed" and c.path == "orders.updated_at" for c in d.changes)
    assert d.required_bump == "major" and d.declared_bump == "minor" and not d.version_ok
    assert diff_contracts(old, _bump("2.0.0", lambda t: t.replace("      - name: updated_at\n        logicalType: timestamp\n", ""))).version_ok


def test_additive_and_relaxing_changes():
    old = _bump("1.0.0")
    added = _bump("1.1.0", lambda t: t.replace("      - name: updated_at\n", "      - name: region\n        logicalType: string\n      - name: updated_at\n"))
    d = diff_contracts(old, added)
    assert d.required_bump == "minor" and d.version_ok and d.changes[0].kind == "additive"

    relaxed = _bump("1.0.1", lambda t: t.replace("      - name: status\n        logicalType: string\n        required: true",
                                                 "      - name: status\n        logicalType: string"))
    d = diff_contracts(old, relaxed)
    assert any(c.code == "required-relaxed" and c.kind == "breaking" for c in d.changes)
    assert not d.version_ok


def test_sla_loosening_is_breaking_and_identical_is_clean():
    old = _bump("1.0.0")
    assert diff_contracts(old, _bump("1.0.0")).required_bump == "none"
    looser = _bump("2.0.0", lambda t: t.replace("value: 24", "value: 48"))
    assert any(c.code == "sla-changed" for c in diff_contracts(old, looser).changes)
