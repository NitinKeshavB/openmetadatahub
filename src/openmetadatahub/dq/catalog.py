"""Built-in metric catalog compiled to Databricks SQL.

Every metric is one of two shapes:
  * row-wise:  yields (total_rows, failed_rows); passes if failed share <= case.max_failed_pct
  * scalar:    yields one numeric `observed`; passes if it sits inside params min/max/equals

All values reach SQL through `sql_literal` and all names through `quote_ident`; params are
validated up front so a bad contract fails before any query runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Callable,
)

from ..common import (
    quote_fqn,
    quote_ident,
    sql_literal,
)
from .models import (
    Dimension,
    Scope,
    TestCase,
)


class MetricError(ValueError):
    pass


@dataclass(frozen=True)
class Metric:
    name: str
    dimension: Dimension
    scope: Scope
    kind: str  # "rowwise" | "scalar"
    description: str
    build: Callable[[TestCase], str]
    required: tuple[str, ...] = ()


def _where(case: TestCase, extra: str | None = None) -> str:
    clauses = []
    if case.where:
        _check_fragment(case.where)
        clauses.append(f"({case.where})")
    if extra:
        clauses.append(f"({extra})")
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def _check_fragment(fragment: str) -> None:
    lowered = fragment.lower()
    if ";" in fragment or "--" in fragment or "/*" in fragment:
        raise MetricError("filter must be a single expression without comments or semicolons")
    for word in (" drop ", " delete ", " insert ", " update ", " merge ", " grant ", " alter "):
        if word in f" {lowered} ":
            raise MetricError(f"filter contains forbidden keyword:{word.strip()}")


def _col(case: TestCase) -> str:
    if not case.column:
        raise MetricError(f"metric {case.metric} needs a column")
    return quote_ident(case.column)


def _rowwise(case: TestCase, fail_predicate: str) -> str:
    t = quote_fqn(case.table)
    return (
        "SELECT COUNT(*) AS total_rows, "
        f"COALESCE(SUM(CASE WHEN {fail_predicate} THEN 1 ELSE 0 END), 0) AS failed_rows "
        f"FROM {t}{_where(case)}"
    )


def _need(case: TestCase, *keys: str) -> None:
    missing = [k for k in keys if k not in case.params]
    if missing:
        raise MetricError(f"metric {case.metric} missing params: {', '.join(missing)}")


# ---------- column, row-wise ----------


def _not_null(case: TestCase) -> str:
    return _rowwise(case, f"{_col(case)} IS NULL")


def _in_set(case: TestCase) -> str:
    _need(case, "values")
    vals = ", ".join(sql_literal(v) for v in case.params["values"])
    return _rowwise(case, f"{_col(case)} IS NOT NULL AND {_col(case)} NOT IN ({vals})")


def _not_in_set(case: TestCase) -> str:
    _need(case, "values")
    vals = ", ".join(sql_literal(v) for v in case.params["values"])
    return _rowwise(case, f"{_col(case)} IN ({vals})")


def _regex(case: TestCase) -> str:
    _need(case, "pattern")
    c = _col(case)
    return _rowwise(case, f"{c} IS NOT NULL AND NOT (CAST({c} AS STRING) RLIKE {sql_literal(case.params['pattern'])})")


def _between(case: TestCase) -> str:
    c = _col(case)
    bounds = []
    if "min" in case.params:
        bounds.append(f"{c} < {sql_literal(case.params['min'])}")
    if "max" in case.params:
        bounds.append(f"{c} > {sql_literal(case.params['max'])}")
    if not bounds:
        raise MetricError("column_values_between needs min and/or max")
    return _rowwise(case, f"{c} IS NOT NULL AND ({' OR '.join(bounds)})")


def _length_between(case: TestCase) -> str:
    c = _col(case)
    bounds = []
    if "min" in case.params:
        bounds.append(f"LENGTH(CAST({c} AS STRING)) < {sql_literal(case.params['min'])}")
    if "max" in case.params:
        bounds.append(f"LENGTH(CAST({c} AS STRING)) > {sql_literal(case.params['max'])}")
    if not bounds:
        raise MetricError("column_length_between needs min and/or max")
    return _rowwise(case, f"{c} IS NOT NULL AND ({' OR '.join(bounds)})")


def _unique(case: TestCase) -> str:
    c = _col(case)
    t = quote_fqn(case.table)
    w = _where(case, f"{c} IS NOT NULL")
    return (
        "SELECT COALESCE(SUM(n), 0) AS total_rows, "
        "COALESCE(SUM(CASE WHEN n > 1 THEN n ELSE 0 END), 0) AS failed_rows "
        f"FROM (SELECT COUNT(*) AS n FROM {t}{w} GROUP BY {c})"
    )


def _fk_exists(case: TestCase) -> str:
    _need(case, "ref_table", "ref_column")
    c = _col(case)
    ref = quote_fqn(case.params["ref_table"])
    rc = quote_ident(case.params["ref_column"])
    return _rowwise(case, f"{c} IS NOT NULL AND {c} NOT IN (SELECT {rc} FROM {ref} WHERE {rc} IS NOT NULL)")


# ---------- scalar ----------


def _row_count(case: TestCase) -> str:
    return f"SELECT COUNT(*) AS observed FROM {quote_fqn(case.table)}{_where(case)}"


def _null_pct(case: TestCase) -> str:
    c = _col(case)
    return (
        f"SELECT 100.0 * SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS observed "
        f"FROM {quote_fqn(case.table)}{_where(case)}"
    )


def _stat(fn: str) -> Callable[[TestCase], str]:
    def build(case: TestCase) -> str:
        return f"SELECT {fn}({_col(case)}) AS observed FROM {quote_fqn(case.table)}{_where(case)}"

    return build


def _freshness(case: TestCase) -> str:
    _need(case, "max_age_hours")
    c = _col(case)
    return (
        f"SELECT (UNIX_TIMESTAMP(CURRENT_TIMESTAMP()) - UNIX_TIMESTAMP(MAX({c}))) / 3600.0 AS observed "
        f"FROM {quote_fqn(case.table)}{_where(case)}"
    )


def _column_count(case: TestCase) -> str:
    cat, sch, tbl = case.table.split(".")
    return (
        f"SELECT COUNT(*) AS observed FROM {quote_ident(cat)}.information_schema.columns "
        f"WHERE table_schema = {sql_literal(sch)} AND table_name = {sql_literal(tbl)}"
    )


def _custom_scalar(case: TestCase) -> str:
    _need(case, "sql")
    sql = case.params["sql"].strip()
    low = sql.lower()
    if not (low.startswith("select") or low.startswith("with")) or ";" in sql or "--" in sql:
        raise MetricError("custom_sql must be a single SELECT/WITH statement without ';' or comments")
    return f"SELECT CAST(observed AS DOUBLE) AS observed FROM ({sql}) q(observed)"


_SCALAR_BOUNDS = {
    "row_count": ("Table row count within bounds", Dimension.VOLUME, Scope.TABLE, _row_count, ()),
    "freshness_hours": (
        "Newest value of a timestamp column is recent enough",
        Dimension.TIMELINESS,
        Scope.COLUMN,
        _freshness,
        ("max_age_hours",),
    ),
    "column_count": ("Table column count within bounds", Dimension.CONSISTENCY, Scope.TABLE, _column_count, ()),
    "null_pct": ("Share of nulls in a column within bounds", Dimension.COMPLETENESS, Scope.COLUMN, _null_pct, ()),
    "column_min": ("Column minimum within bounds", Dimension.ACCURACY, Scope.COLUMN, _stat("MIN"), ()),
    "column_max": ("Column maximum within bounds", Dimension.ACCURACY, Scope.COLUMN, _stat("MAX"), ()),
    "column_mean": ("Column mean within bounds", Dimension.ACCURACY, Scope.COLUMN, _stat("AVG"), ()),
    "column_stddev": (
        "Column standard deviation within bounds",
        Dimension.ACCURACY,
        Scope.COLUMN,
        _stat("STDDEV"),
        (),
    ),
    "column_sum": ("Column sum within bounds", Dimension.ACCURACY, Scope.COLUMN, _stat("SUM"), ()),
    "custom_sql": ("Single-value SELECT within bounds", Dimension.CUSTOM, Scope.TABLE, _custom_scalar, ("sql",)),
}

_ROWWISE = {
    "column_not_null": ("No nulls", Dimension.COMPLETENESS, Scope.COLUMN, _not_null, ()),
    "column_unique": ("No duplicate non-null values", Dimension.UNIQUENESS, Scope.COLUMN, _unique, ()),
    "column_in_set": ("Values restricted to a set", Dimension.VALIDITY, Scope.COLUMN, _in_set, ("values",)),
    "column_not_in_set": ("Values avoid a forbidden set", Dimension.VALIDITY, Scope.COLUMN, _not_in_set, ("values",)),
    "column_matches_regex": (
        "Values match a regular expression",
        Dimension.VALIDITY,
        Scope.COLUMN,
        _regex,
        ("pattern",),
    ),
    "column_between": ("Values inside a range", Dimension.ACCURACY, Scope.COLUMN, _between, ()),
    "column_length_between": ("String length inside a range", Dimension.VALIDITY, Scope.COLUMN, _length_between, ()),
    "column_fk_exists": (
        "Values exist in a reference table",
        Dimension.INTEGRITY,
        Scope.COLUMN,
        _fk_exists,
        ("ref_table", "ref_column"),
    ),
}

METRICS: dict[str, Metric] = {}
for _n, (_d, _dim, _scope, _b, _req) in _SCALAR_BOUNDS.items():
    METRICS[_n] = Metric(_n, _dim, _scope, "scalar", _d, _b, _req)
for _n, (_d, _dim, _scope, _b, _req) in _ROWWISE.items():
    METRICS[_n] = Metric(_n, _dim, _scope, "rowwise", _d, _b, _req)


def get_metric(name: str) -> Metric:
    try:
        return METRICS[name]
    except KeyError:
        raise MetricError(f"unknown metric {name!r}; available: {', '.join(sorted(METRICS))}") from None


def describe_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": m.name,
            "dimension": m.dimension.value,
            "scope": m.scope.value,
            "kind": m.kind,
            "description": m.description,
            "required_params": list(m.required),
        }
        for m in sorted(METRICS.values(), key=lambda m: (m.dimension.value, m.name))
    ]
