"""SqlExecutor over a SparkSession, for code running *inside* Databricks.

Inside a job the Spark session is the warehouse: no tokens, no hostnames, and the job's own run-as
identity decides what can be read. Only read statements are accepted here; results are written by
ResultStore, never through this executor.
"""

from __future__ import annotations

from typing import Any

from ..common import Row

_READ_ONLY = ("select", "show", "with", "describe")


class SparkSqlExecutor:
    def __init__(self, spark: Any) -> None:
        self._spark = spark

    def query(self, sql: str) -> list[Row]:
        words = sql.split(None, 1)
        if not words or words[0].lower() not in _READ_ONLY:
            raise PermissionError(f"read-only executor refused statement: {sql[:60]!r}")
        return [r.asDict(recursive=True) for r in self._spark.sql(sql).collect()]


def get_spark() -> Any:
    from pyspark.sql import SparkSession

    return SparkSession.builder.getOrCreate()
