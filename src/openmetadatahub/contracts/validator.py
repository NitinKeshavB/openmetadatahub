"""Validate a data contract's declared schema against live Unity Catalog metadata."""

from __future__ import annotations

from pydantic import BaseModel

from ..common import Severity, SqlExecutor, quote_ident, sql_literal
from .odcs import DataContract

_TYPE_FAMILY = {
    "string": {"STRING", "VARCHAR", "CHAR"},
    "integer": {"TINYINT", "SMALLINT", "INT", "INTEGER", "BIGINT", "LONG"},
    "number": {"FLOAT", "DOUBLE", "DECIMAL", "REAL"},
    "boolean": {"BOOLEAN"},
    "date": {"DATE"},
    "timestamp": {"TIMESTAMP", "TIMESTAMP_NTZ"},
    "array": {"ARRAY"},
    "object": {"STRUCT", "MAP"},
}


class SchemaIssue(BaseModel):
    table: str
    column: str | None
    code: str
    severity: Severity
    message: str


def _base_type(data_type: str) -> str:
    return data_type.upper().split("(")[0].split("<")[0].strip()


def fetch_columns(executor: SqlExecutor, table: str) -> list[dict]:
    cat, sch, tbl = table.split(".")
    return executor.query(
        "SELECT column_name, data_type, is_nullable "
        f"FROM {quote_ident(cat)}.information_schema.columns "
        f"WHERE table_schema = {sql_literal(sch)} AND table_name = {sql_literal(tbl)} "
        "ORDER BY ordinal_position"
    )


def validate_schema(contract: DataContract, executor: SqlExecutor, namespace: str | None = None) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    for obj in contract.schema_:
        table = contract.qualified_table(obj, namespace)
        actual = {r["column_name"].lower(): r for r in fetch_columns(executor, table)}
        if not actual:
            issues.append(SchemaIssue(table=table, column=None, code="table-missing", severity=Severity.CRITICAL,
                                      message="table not found or no columns visible to this principal"))
            continue
        declared = {p.name.lower() for p in obj.properties}
        for prop in obj.properties:
            row = actual.get(prop.name.lower())
            if row is None:
                issues.append(SchemaIssue(table=table, column=prop.name, code="column-missing", severity=Severity.CRITICAL,
                                          message="declared in contract but absent from the table"))
                continue
            got = _base_type(str(row["data_type"]))
            if prop.physicalType:
                want = _base_type(prop.physicalType)
                if want != got:
                    issues.append(SchemaIssue(table=table, column=prop.name, code="type-mismatch", severity=Severity.ERROR,
                                              message=f"physicalType {want} declared, table has {got}"))
            elif prop.logicalType and prop.logicalType.lower() in _TYPE_FAMILY:
                if got not in _TYPE_FAMILY[prop.logicalType.lower()]:
                    issues.append(SchemaIssue(table=table, column=prop.name, code="type-mismatch", severity=Severity.ERROR,
                                              message=f"logicalType {prop.logicalType} expects one of "
                                                      f"{sorted(_TYPE_FAMILY[prop.logicalType.lower()])}, table has {got}"))
            if (prop.required or prop.primaryKey) and str(row.get("is_nullable", "YES")).upper() == "YES":
                issues.append(SchemaIssue(table=table, column=prop.name, code="nullable-but-required", severity=Severity.WARNING,
                                          message="contract requires a value but the column has no NOT NULL constraint; "
                                                  "the not-null data quality test is the only enforcement"))
        for name in sorted(set(actual) - declared):
            issues.append(SchemaIssue(table=table, column=name, code="undeclared-column", severity=Severity.INFO,
                                      message="present in the table but not declared in the contract"))
    return issues
