"""Compile a data contract into executable data quality test cases."""

from __future__ import annotations

from ..common import Severity
from ..dq.models import TestCase
from .odcs import DataContract, QualityRule, SchemaObject

_SEVERITY = {
    "info": Severity.INFO, "warning": Severity.WARNING, "warn": Severity.WARNING,
    "error": Severity.ERROR, "critical": Severity.CRITICAL,
}

_UNIT_HOURS = {"h": 1.0, "hour": 1.0, "hours": 1.0, "d": 24.0, "day": 24.0, "days": 24.0,
               "m": 1 / 60, "min": 1 / 60, "minutes": 1 / 60, "y": 24 * 365.0, "year": 24 * 365.0, "years": 24 * 365.0}


def _sev(rule: QualityRule, default: Severity = Severity.ERROR) -> Severity:
    return _SEVERITY.get((rule.severity or "").lower(), default)


def _bounds(rule: QualityRule) -> dict:
    p: dict = {}
    if rule.mustBe is not None:
        p["equals"] = rule.mustBe
    if rule.mustNotBe is not None:
        p["ne"] = rule.mustNotBe
    if rule.mustBeGreaterThan is not None:
        p["gt"] = rule.mustBeGreaterThan
    if rule.mustBeGreaterOrEqualTo is not None:
        p["min"] = rule.mustBeGreaterOrEqualTo
    if rule.mustBeLessThan is not None:
        p["lt"] = rule.mustBeLessThan
    if rule.mustBeLessOrEqualTo is not None:
        p["max"] = rule.mustBeLessOrEqualTo
    if rule.mustBeBetween:
        p["min"], p["max"] = rule.mustBeBetween[0], rule.mustBeBetween[1]
    if rule.mustNotBeBetween:
        p["outside"] = list(rule.mustNotBeBetween)
    return p


def _tolerance(rule: QualityRule) -> dict:
    """Row-wise rules express a tolerated amount of bad rows in rows or percent."""
    b = _bounds(rule)
    limit = b.get("equals", b.get("max", b.get("lt")))
    if limit is None:
        return {}
    return {"max_failed_pct": float(limit)} if rule.unit == "percent" else {"max_failed_rows": int(limit)}


def _rule_cases(rule: QualityRule, table: str, column: str | None, contract: DataContract, idx: int) -> list[TestCase]:
    base = dict(table=table, severity=_sev(rule), source=contract.id)
    label = rule.name or rule.metric or rule.type or f"rule{idx}"
    name = f"{table.split('.')[-1]}{'.' + column if column else ''}:{label}"
    args = rule.arguments or {}

    if rule.type == "sql" and rule.query:
        return [TestCase(name=name, metric="custom_sql", params={"sql": rule.query, **_bounds(rule)}, **base)]
    if rule.type not in (None, "library"):
        return []  # text/custom rules are documentation, not executable

    m = rule.metric
    tol = _tolerance(rule)
    if m == "nullValues" and column:
        return [TestCase(name=name, metric="column_not_null", column=column, **tol, **base)]
    if m == "duplicateValues" and column:
        return [TestCase(name=name, metric="column_unique", column=column, **tol, **base)]
    if m == "missingValues" and column and "missingValues" in args:
        return [TestCase(name=name, metric="column_not_in_set", column=column, params={"values": args["missingValues"]}, **tol, **base)]
    if m == "invalidValues" and column:
        if "validValues" in args:
            return [TestCase(name=name, metric="column_in_set", column=column, params={"values": args["validValues"]}, **tol, **base)]
        if "pattern" in args:
            return [TestCase(name=name, metric="column_matches_regex", column=column, params={"pattern": args["pattern"]}, **tol, **base)]
        rng = {k: args[k2] for k, k2 in (("min", "minValue"), ("max", "maxValue")) if k2 in args}
        if rng:
            return [TestCase(name=name, metric="column_between", column=column, params=rng, **tol, **base)]
    if m == "rowCount":
        return [TestCase(name=name, metric="row_count", params=_bounds(rule), **base)]
    return []


def contract_to_tests(contract: DataContract, namespace: str | None = None) -> list[TestCase]:
    cases: list[TestCase] = []
    for obj in contract.schema_:
        table = contract.qualified_table(obj, namespace)
        short = obj.name
        for prop in obj.properties:
            if prop.required or prop.primaryKey:
                cases.append(TestCase(name=f"{short}.{prop.name}:required", table=table, column=prop.name,
                                      metric="column_not_null", source=contract.id))
            if prop.unique or prop.primaryKey:
                cases.append(TestCase(name=f"{short}.{prop.name}:unique", table=table, column=prop.name,
                                      metric="column_unique", source=contract.id))
            for i, rule in enumerate(prop.quality):
                cases += _rule_cases(rule, table, prop.name, contract, i)
        for i, rule in enumerate(obj.quality):
            cases += _rule_cases(rule, table, None, contract, i)
        cases += _sla_cases(contract, obj, table)
    return cases


def _sla_cases(contract: DataContract, obj: SchemaObject, table: str) -> list[TestCase]:
    out = []
    for sla in contract.slaProperties:
        if sla.property not in ("freshness", "latency") or not sla.element or "." not in sla.element:
            continue
        tbl_name, col = sla.element.split(".", 1)
        if tbl_name != obj.name:
            continue
        try:
            hours = float(sla.value) * _UNIT_HOURS[(sla.unit or "h").lower()]
        except (ValueError, KeyError):
            continue
        out.append(TestCase(name=f"{obj.name}.{col}:sla-{sla.property}", table=table, column=col,
                            metric="freshness_hours", params={"max_age_hours": hours},
                            severity=Severity.ERROR, source=contract.id))
    return out
