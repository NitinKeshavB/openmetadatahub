"""Run every built-in metric on a real Spark engine against data whose answers were worked out by hand."""

import statistics

import pytest

pytest.importorskip("pyspark")

from spark_support import (  # noqa: E402
    LocalExecutor,
    refresh_information_schema,
)

from openmetadatahub.dq import (  # noqa: E402
    Status,
    TestCase,
    run_case,
)

AMOUNTS = [10.0, 20.0, 30.0, 40.0, 50.0, -5.0, 60.0, 70.0, 80.0, 90.0]


@pytest.fixture(scope="module")
def lake(spark):
    spark.sql("CREATE DATABASE IF NOT EXISTS sales")
    spark.sql("DROP TABLE IF EXISTS sales.orders")
    rows = [
        (1, "a@x.com", "NEW"),
        (2, "b@x.com", "PAID"),
        (3, "bad-email", "PAID"),
        (4, None, "NEW"),
        (5, "e@x.com", "WEIRD"),
        (6, "f@x.com", "SHIPPED"),
        (7, "g@x.com", "NEW"),
        (7, "h@x.com", "NEW"),
        (8, "i@x.com", "PAID"),
        (9, "j@x.com", None),
    ]
    df = spark.createDataFrame(
        [(*r, a) for r, a in zip(rows, AMOUNTS)], "id int, email string, status string, amount double"
    )
    df.createOrReplaceTempView("_o")
    spark.sql("CREATE TABLE sales.orders AS SELECT *, current_timestamp() - INTERVAL 2 HOURS AS updated_at FROM _o")
    spark.sql("DROP TABLE IF EXISTS sales.dim")
    spark.createDataFrame([(i,) for i in range(1, 9)], "id int").write.saveAsTable("sales.dim")
    refresh_information_schema(spark)
    return LocalExecutor(spark)


def case(metric, column=None, **params):
    return TestCase(
        name=metric,
        table="spark_catalog.sales.orders",
        column=column,
        metric=metric,
        params=params,
        max_failed_pct=100,
    )


# metric -> (case, expected). expected is ("rows", total, failed) or ("value", observed)
EXPECT = {
    "column_not_null": (case("column_not_null", "status"), ("rows", 10, 1)),
    "column_unique": (case("column_unique", "id"), ("rows", 10, 2)),
    "column_in_set": (case("column_in_set", "status", values=["NEW", "PAID", "SHIPPED"]), ("rows", 10, 1)),
    "column_not_in_set": (case("column_not_in_set", "status", values=["WEIRD"]), ("rows", 10, 1)),
    "column_matches_regex": (case("column_matches_regex", "email", pattern="^[^@]+@[^@]+$"), ("rows", 10, 1)),
    "column_between": (case("column_between", "amount", min=0, max=100), ("rows", 10, 1)),
    "column_length_between": (case("column_length_between", "status", min=3, max=5), ("rows", 10, 1)),
    "column_fk_exists": (
        case("column_fk_exists", "id", ref_table="spark_catalog.sales.dim", ref_column="id"),
        ("rows", 10, 1),
    ),
    "row_count": (case("row_count", min=1), ("value", 10.0)),
    "null_pct": (case("null_pct", "email", max=100), ("value", 10.0)),
    "column_min": (case("column_min", "amount", min=-100), ("value", -5.0)),
    "column_max": (case("column_max", "amount", max=1000), ("value", 90.0)),
    "column_mean": (case("column_mean", "amount", max=1000), ("value", sum(AMOUNTS) / 10)),
    "column_sum": (case("column_sum", "amount", max=1000), ("value", sum(AMOUNTS))),
    "column_stddev": (case("column_stddev", "amount", max=1000), ("value", statistics.stdev(AMOUNTS))),
    "column_count": (case("column_count", min=1), ("value", 5.0)),
    "custom_sql": (
        case("custom_sql", sql="SELECT COUNT(*) FROM spark_catalog.sales.orders WHERE amount < 0", max=10),
        ("value", 1.0),
    ),
}


def test_every_metric_is_covered():
    from openmetadatahub.dq import METRICS

    assert set(EXPECT) | {"freshness_hours"} == set(METRICS)


@pytest.mark.parametrize("metric", sorted(EXPECT))
def test_metric_returns_the_hand_computed_answer_on_spark(lake, metric):
    c, (kind, *vals) = EXPECT[metric]
    r = run_case(c, lake)
    assert r.status != Status.ABORTED, r.message
    if kind == "rows":
        assert (r.total_rows, r.failed_rows) == tuple(vals)
    else:
        assert r.observed == pytest.approx(vals[0])


def test_freshness_is_measured_in_hours(lake):
    r = run_case(case("freshness_hours", "updated_at", max_age_hours=24), lake)
    assert r.status == Status.SUCCESS and 1.9 < r.observed < 2.5
    assert run_case(case("freshness_hours", "updated_at", max_age_hours=1), lake).status == Status.FAILED


def test_injection_attempt_in_values_is_inert_on_a_real_engine(lake, spark):
    evil = case("column_in_set", "status", values=["x'); DROP TABLE sales.orders; --"])
    r = run_case(evil, lake)
    assert r.status in (Status.SUCCESS, Status.FAILED)
    assert spark.table("sales.orders").count() == 10
