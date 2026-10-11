import pytest
import sqlglot

from openmetadatahub.common import Severity, quote_fqn
from openmetadatahub.dq import METRICS, MetricError, Status, TestCase, run_case, run_cases, summarise


def _case(metric, column="email", **params):
    return TestCase(name=f"t:{metric}", table="main.sales.orders", column=column, metric=metric, params=params)


SAMPLE_PARAMS = {
    "row_count": {"min": 1}, "freshness_hours": {"max_age_hours": 24}, "column_count": {"min": 1},
    "null_pct": {"max": 5}, "column_min": {"min": 0}, "column_max": {"max": 9}, "column_mean": {"min": 0},
    "column_stddev": {"max": 9}, "column_sum": {"min": 0}, "custom_sql": {"sql": "SELECT 1"},
    "column_not_null": {}, "column_unique": {}, "column_in_set": {"values": ["a", "b"]},
    "column_not_in_set": {"values": ["x"]}, "column_matches_regex": {"pattern": "^a"},
    "column_between": {"min": 0, "max": 5}, "column_length_between": {"min": 1, "max": 5},
    "column_fk_exists": {"ref_table": "main.ref.dim", "ref_column": "id"},
}


def test_every_metric_has_sample_params():
    assert set(SAMPLE_PARAMS) == set(METRICS)


@pytest.mark.parametrize("metric", sorted(METRICS))
def test_generated_sql_parses_as_databricks(metric):
    sql = METRICS[metric].build(_case(metric, **SAMPLE_PARAMS[metric]))
    sqlglot.parse_one(sql, read="databricks")  # raises on syntax errors


def test_values_are_escaped_not_interpolated():
    sql = METRICS["column_in_set"].build(_case("column_in_set", values=["o'brien", "x') OR 1=1 --"]))
    assert "'o''brien'" in sql
    assert "'x'') OR 1=1 --'" in sql
    sqlglot.parse_one(sql, read="databricks")


def test_identifiers_cannot_escape_quoting():
    with pytest.raises(ValueError):
        quote_fqn("main.sales.orders`; DROP TABLE x; --")
    with pytest.raises(ValueError):
        quote_fqn("only.two")


def test_filter_and_custom_sql_guards():
    bad = TestCase(name="n", table="main.s.t", metric="row_count", where="1=1; DROP TABLE x")
    assert run_case(bad, None).status == Status.ABORTED  # rejected before any query
    with pytest.raises(MetricError):
        METRICS["custom_sql"].build(_case("custom_sql", sql="DELETE FROM x"))
    with pytest.raises(MetricError):
        METRICS["custom_sql"].build(_case("custom_sql", sql="SELECT 1; SELECT 2"))


def test_rowwise_pass_fail_and_tolerance(fake):
    fake.when("IS NULL", [{"total_rows": 1000, "failed_rows": 3}])
    strict = _case("column_not_null")
    assert run_case(strict, fake).status == Status.FAILED
    tolerant = strict.model_copy(update={"max_failed_pct": 0.5})
    assert run_case(tolerant, fake).status == Status.SUCCESS
    by_rows = strict.model_copy(update={"max_failed_rows": 2})
    assert run_case(by_rows, fake).status == Status.FAILED


def test_scalar_bounds_and_operators(fake):
    fake.when("COUNT(*) AS observed", [{"observed": 10}])
    assert run_case(_case("row_count", column=None, min=5, max=20), fake).status == Status.SUCCESS
    assert run_case(_case("row_count", column=None, gt=10), fake).status == Status.FAILED  # strict
    assert run_case(_case("row_count", column=None, lt=10), fake).status == Status.FAILED
    assert run_case(_case("row_count", column=None, outside=[5, 15]), fake).status == Status.FAILED
    assert run_case(_case("row_count", column=None, equals=10), fake).status == Status.SUCCESS


def test_freshness_uses_max_age(fake):
    fake.when("UNIX_TIMESTAMP", [{"observed": 30.5}])
    r = run_case(_case("freshness_hours", column="updated_at", max_age_hours=24), fake)
    assert r.status == Status.FAILED and r.observed == 30.5


def test_unique_counts_every_row_in_a_duplicate_group(fake):
    fake.when("GROUP BY", [{"total_rows": 10, "failed_rows": 4}])
    r = run_case(_case("column_unique", column="order_id"), fake)
    assert r.failed_rows == 4 and r.failed_pct == 40.0


def test_unrunnable_checks_are_aborted_and_never_pass(fake):
    unknown = TestCase(name="x", table="a.b.c", metric="nope")
    assert run_case(unknown, fake).status == Status.ABORTED
    fake.when("COUNT(*) AS observed", [{"observed": None}])
    assert run_case(_case("row_count", column=None, min=1), fake).status == Status.ABORTED
    boom = type(fake)().when("COUNT", RuntimeError("warehouse down"))
    r = run_case(_case("row_count", column=None, min=1), boom)
    assert r.status == Status.ABORTED and "warehouse down" in r.message
    assert summarise("a.b.c", [r]).score == 0.0


def test_score_is_severity_weighted_by_dimension(fake):
    fake.when("IS NULL", [{"total_rows": 100, "failed_rows": 0}])
    fake.when("GROUP BY", [{"total_rows": 100, "failed_rows": 50}])
    cases = [
        _case("column_not_null", column="a"),
        TestCase(name="u", table="main.sales.orders", column="id", metric="column_unique", severity=Severity.WARNING),
    ]
    rep = summarise("main.sales.orders", run_cases(cases, fake))
    # not_null (error=4) passes, unique (warning=2) fails -> 4/6
    assert rep.score == pytest.approx(66.67, abs=0.01)
    assert rep.by_dimension == {"completeness": 100.0, "uniqueness": 0.0}
    assert (rep.passed, rep.failed, rep.aborted) == (1, 1, 0)
