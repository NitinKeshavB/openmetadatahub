"""Governance results as Delta tables: the system of record for runs, scores and decisions.

Tables are created with plain `CREATE TABLE` (Delta is the Databricks default). With no catalog the
store targets `schema.table`, which is what the legacy hive_metastore and local Spark use.
Everything is append-only except contract_versions' baseline logic, which reads, never updates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..common import quote_ident, sql_literal

TABLES: dict[str, str] = {
    "contract_runs": (
        "run_id STRING, run_ts TIMESTAMP, contract_id STRING, contract_version STRING, contract_status STRING, "
        "tables_checked INT, tests_total INT, tests_failed INT, tests_aborted INT, schema_issues INT, "
        "version_ok BOOLEAN, blocking INT, decision STRING, detail STRING"
    ),
    "dq_results": (
        "run_id STRING, run_ts TIMESTAMP, contract_id STRING, test_name STRING, table_name STRING, column_name STRING, "
        "metric STRING, dimension STRING, severity STRING, status STRING, observed DOUBLE, total_rows BIGINT, "
        "failed_rows BIGINT, failed_pct DOUBLE, message STRING, sql_text STRING"
    ),
    "quality_scores": (
        "run_id STRING, run_ts TIMESTAMP, table_name STRING, score DOUBLE, by_dimension STRING, "
        "passed INT, failed INT, aborted INT"
    ),
    "schema_issues": (
        "run_id STRING, run_ts TIMESTAMP, contract_id STRING, table_name STRING, column_name STRING, "
        "code STRING, severity STRING, message STRING"
    ),
    "contract_versions": (
        "contract_id STRING, version STRING, sha256 STRING, yaml STRING, first_seen_run_id STRING, "
        "first_seen_ts TIMESTAMP, version_ok BOOLEAN, diff_summary STRING"
    ),
    "access_audits": (
        "run_id STRING, run_ts TIMESTAMP, table_name STRING, contract_id STRING, findings INT, blocking INT, caveats STRING"
    ),
    "access_findings": (
        "run_id STRING, run_ts TIMESTAMP, table_name STRING, code STRING, severity STRING, principal STRING, "
        "column_name STRING, message STRING, remedy STRING"
    ),
    "ai_assessments": (
        "run_id STRING, run_ts TIMESTAMP, asset_id STRING, asset_name STRING, stage STRING, risk_class STRING, "
        "blocked BOOLEAN, checks STRING, control_coverage STRING"
    ),
    "run_events": (
        "run_id STRING, seq INT, level STRING, message STRING, logged_at TIMESTAMP, context STRING"
    ),
}


def _columns(ddl: str) -> list[str]:
    return [part.split()[0] for part in ddl.split(", ")]


class ResultStore:
    def __init__(self, spark: Any, catalog: str | None, schema: str) -> None:
        self.spark, self.catalog, self.schema = spark, catalog or None, schema

    def fqn(self, table: str) -> str:
        parts = [self.catalog, self.schema, table] if self.catalog else [self.schema, table]
        return ".".join(quote_ident(p) for p in parts)

    def ensure(self) -> None:
        schema_fqn = ".".join(quote_ident(p) for p in ([self.catalog] if self.catalog else []) + [self.schema])
        self.spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema_fqn}")
        for name, ddl in TABLES.items():
            self.spark.sql(f"CREATE TABLE IF NOT EXISTS {self.fqn(name)} ({ddl})")

    def append(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        from pyspark.sql.types import StructType

        ddl = TABLES[table]
        cols = _columns(ddl)
        data = [tuple(r.get(c) for c in cols) for r in rows]
        df = self.spark.createDataFrame(data, schema=StructType.fromDDL(ddl))
        df.write.mode("append").saveAsTable(self.fqn(table).replace("`", ""))

    # ---- reads used by enforcement and the AI gate ----

    def _one(self, sql: str) -> dict | None:
        rows = self.spark.sql(sql).limit(1).collect()
        return rows[0].asDict() if rows else None

    def latest_contract_version(self, contract_id: str) -> dict | None:
        return self._one(f"SELECT * FROM {self.fqn('contract_versions')} WHERE contract_id = {sql_literal(contract_id)} "
                         "ORDER BY first_seen_ts DESC")

    def baseline_contract_version(self, contract_id: str) -> dict | None:
        """Last version that complied with the version-bump rule: what new changes are judged against."""
        return self._one(f"SELECT * FROM {self.fqn('contract_versions')} WHERE contract_id = {sql_literal(contract_id)} "
                         "AND version_ok ORDER BY first_seen_ts DESC")

    def latest_quality_scores(self, max_age_hours: float) -> dict[str, float]:
        q = (f"SELECT table_name, score FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY run_ts DESC) rn "
             f"FROM {self.fqn('quality_scores')} WHERE run_ts >= current_timestamp() - INTERVAL {int(max_age_hours)} HOURS) WHERE rn = 1")
        return {r["table_name"]: float(r["score"]) for r in self.spark.sql(q).collect()}

    def latest_access_findings(self, max_age_hours: float) -> dict[str, list[dict]]:
        """Findings from each table's most recent audit. A table whose latest audit found nothing maps to []
        so an older audit's findings can never resurface."""
        audits = (f"SELECT table_name, run_id FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY run_ts DESC) rn "
                  f"FROM {self.fqn('access_audits')} WHERE run_ts >= current_timestamp() - INTERVAL {int(max_age_hours)} HOURS) WHERE rn = 1")
        out: dict[str, list[dict]] = {r["table_name"]: [] for r in self.spark.sql(audits).collect()}
        q = (f"SELECT f.* FROM {self.fqn('access_findings')} f JOIN ({audits}) a "
             "ON f.table_name = a.table_name AND f.run_id = a.run_id")
        for r in self.spark.sql(q).collect():
            out.setdefault(r["table_name"], []).append(r.asDict())
        return out


def now_utc() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)
