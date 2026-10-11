"""Execute test cases and summarise them into a quality report."""

from __future__ import annotations

from typing import Iterable

from ..common import Severity, SqlExecutor
from .catalog import MetricError, get_metric
from .models import Dimension, QualityReport, Status, TestCase, TestResult

SEVERITY_WEIGHT = {Severity.INFO: 1, Severity.WARNING: 2, Severity.ERROR: 4, Severity.CRITICAL: 8}


def _within(observed: float, params: dict) -> bool:
    if "equals" in params and observed != params["equals"]:
        return False
    if "ne" in params and observed == params["ne"]:
        return False
    if "min" in params and observed < params["min"]:
        return False
    if "max" in params and observed > params["max"]:
        return False
    if "gt" in params and not observed > params["gt"]:
        return False
    if "lt" in params and not observed < params["lt"]:
        return False
    if "outside" in params:
        lo, hi = params["outside"]
        if lo <= observed <= hi:
            return False
    return True


def run_case(case: TestCase, executor: SqlExecutor) -> TestResult:
    try:
        metric = get_metric(case.metric)
        sql = metric.build(case)
    except MetricError as exc:
        return TestResult(case=case, dimension=Dimension.CUSTOM, status=Status.ABORTED, message=str(exc))

    try:
        rows = executor.query(sql)
    except Exception as exc:  # warehouse boundary: surface, do not crash the whole run
        return TestResult(case=case, dimension=metric.dimension, status=Status.ABORTED, sql=sql,
                          message=f"query failed: {exc}")
    if not rows:
        return TestResult(case=case, dimension=metric.dimension, status=Status.ABORTED, sql=sql,
                          message="query returned no rows")
    row = rows[0]

    if metric.kind == "rowwise":
        total = int(row.get("total_rows") or 0)
        failed = int(row.get("failed_rows") or 0)
        pct = (100.0 * failed / total) if total else 0.0
        if case.max_failed_rows is not None:
            ok = failed <= case.max_failed_rows
            tolerance = f"{case.max_failed_rows} rows"
        else:
            ok = pct <= case.max_failed_pct
            tolerance = f"{case.max_failed_pct}%"
        return TestResult(
            case=case, dimension=metric.dimension, status=Status.SUCCESS if ok else Status.FAILED,
            total_rows=total, failed_rows=failed, failed_pct=round(pct, 4), sql=sql,
            message=f"{failed}/{total} rows failed ({pct:.2f}%), tolerance {tolerance}",
        )

    observed = row.get("observed")
    if observed is None:
        return TestResult(case=case, dimension=metric.dimension, status=Status.ABORTED, sql=sql,
                          message="metric produced NULL (empty table or all-null column)")
    observed = float(observed)
    bounds = dict(case.params)
    if case.metric == "freshness_hours":
        bounds = {"max": case.params["max_age_hours"]}
    ok = _within(observed, bounds)
    return TestResult(case=case, dimension=metric.dimension, status=Status.SUCCESS if ok else Status.FAILED,
                      observed=observed, sql=sql, message=f"observed {observed:g}, expected {bounds}")


def run_cases(cases: Iterable[TestCase], executor: SqlExecutor) -> list[TestResult]:
    return [run_case(c, executor) for c in cases]


def summarise(table: str, results: list[TestResult]) -> QualityReport:
    """Severity-weighted pass rate overall and per dimension. Aborted tests count as failures:
    a check that could not run is not evidence the data is good."""

    def score(rs: list[TestResult]) -> float:
        total = sum(SEVERITY_WEIGHT[r.case.severity] for r in rs)
        if not total:
            return 100.0
        good = sum(SEVERITY_WEIGHT[r.case.severity] for r in rs if r.status == Status.SUCCESS)
        return round(100.0 * good / total, 2)

    dims: dict[str, list[TestResult]] = {}
    for r in results:
        dims.setdefault(r.dimension.value, []).append(r)
    return QualityReport(
        table=table,
        score=score(results),
        by_dimension={d: score(rs) for d, rs in sorted(dims.items())},
        passed=sum(r.status == Status.SUCCESS for r in results),
        failed=sum(r.status == Status.FAILED for r in results),
        aborted=sum(r.status == Status.ABORTED for r in results),
        results=results,
    )
