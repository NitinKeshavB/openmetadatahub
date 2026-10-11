"""Local Spark stand-in for a Databricks workspace.

Real: the Spark SQL engine executing every generated query, three-part `spark_catalog.schema.table` names,
and the result-table writes (parquet locally, Delta on Databricks).
Emulated: Unity Catalog. `information_schema` is rebuilt from Spark's catalog, and SHOW GRANTS (unsupported by
open-source Spark) returns canned rows, so grant *semantics* are not verified here.
"""

from __future__ import annotations

from openmetadatahub.jobs.spark import SparkSqlExecutor

CAT = "spark_catalog"

INFO_TABLES = {
    "row_filters": "schema_name string, table_name string, filter_catalog string, filter_schema string, filter_name string, filter_col_usage string",
    "column_masks": "schema_name string, table_name string, column_name string, mask_catalog string, mask_schema string, mask_name string, mask_col_usage string",
    "table_tags": "schema_name string, table_name string, tag_name string, tag_value string",
    "column_tags": "schema_name string, table_name string, column_name string, tag_name string, tag_value string",
}


class LocalExecutor(SparkSqlExecutor):
    """Real Spark for everything except SHOW GRANTS, keyed by securable type: {"TABLE": rows, ...}."""

    def __init__(self, spark, grants: dict | None = None):
        super().__init__(spark)
        self.grants = grants or {}

    def query(self, sql: str):
        if sql.startswith("SHOW GRANTS"):
            for key, rows in self.grants.items():
                if f" ON {key} " in sql or sql.endswith(f" ON {key}"):
                    if isinstance(rows, Exception):
                        raise rows
                    return rows
            return []
        return super().query(sql)


def refresh_information_schema(spark) -> None:
    """Rebuild information_schema.columns from every user table Spark knows about."""
    spark.sql("CREATE DATABASE IF NOT EXISTS information_schema")
    rows = []
    for db in spark.catalog.listDatabases():
        if db.name in ("information_schema", "default") or db.name.startswith("governance"):
            continue
        for t in spark.catalog.listTables(db.name):
            if t.isTemporary:
                continue
            for i, c in enumerate(spark.catalog.listColumns(f"{db.name}.{t.name}")):
                rows.append((db.name, t.name, c.name, c.dataType, "YES" if c.nullable else "NO", i))
    spark.createDataFrame(rows, "table_schema string, table_name string, column_name string, data_type string, "
                                "is_nullable string, ordinal_position int").write.mode("overwrite").saveAsTable("information_schema.columns")
    for name, ddl in INFO_TABLES.items():
        if not spark.catalog.tableExists(f"information_schema.{name}"):
            set_rows(spark, name, [])


def set_rows(spark, info_table: str, rows: list[tuple]) -> None:
    """Replace the contents of information_schema.<table> (parquet tables do not support DELETE)."""
    spark.createDataFrame(rows, INFO_TABLES[info_table]).write.mode("overwrite").saveAsTable(f"information_schema.{info_table}")
