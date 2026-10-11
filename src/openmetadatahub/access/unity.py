"""Read effective access state for a table from Unity Catalog.

Sources (verified against the Databricks docs, Oct 2026):
  * grants        SHOW GRANTS ON TABLE|SCHEMA|CATALOG  -> principal, actionType, objectType, objectKey
                  (information_schema.*_privileges is NOT used: it only shows the caller's own grants)
  * row filters   information_schema.row_filters    (catalog_name, schema_name, table_name, filter_*, filter_col_usage)
  * column masks  information_schema.column_masks   (catalog_name, schema_name, table_name, column_name, mask_*, mask_col_usage)
  * tags          information_schema.table_tags / column_tags
  * columns       information_schema.columns

SHOW GRANTS on another principal's access needs: workspace admin or object owner, or READ METADATA /
MANAGE on the object plus usage on its parents. Without that the grant list is incomplete, and the
snapshot says so in `caveats` instead of reporting a clean result.
"""

from __future__ import annotations

from ..common import SqlExecutor, quote_ident, sql_literal
from .models import AccessSnapshot, ColumnMask, Grant, RowFilter, Tag

_LEVELS = {
    "CATALOG": "catalog",
    "SCHEMA": "schema", "DATABASE": "schema",
    "TABLE": "table", "VIEW": "table", "MATERIALIZED_VIEW": "table", "STREAMING_TABLE": "table",
}


def _g(row: dict, *keys: str, default=None):
    lowered = {k.lower(): v for k, v in row.items()}
    for k in keys:
        if lowered.get(k.lower()) is not None:
            return lowered[k.lower()]
    return default


def _split(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [p.strip().strip("`'\"") for p in str(value).strip("[]").split(",") if p.strip()]


def _normalise_privilege(action: str, level: str) -> str:
    """Legacy output prints USE for the usage privilege; name it by its securable."""
    action = str(action).upper()
    if action == "USE":
        return {"catalog": "USE CATALOG", "schema": "USE SCHEMA"}.get(level, action)
    return action


def snapshot_table(executor: SqlExecutor, table: str) -> AccessSnapshot:
    cat, sch, tbl = table.split(".")
    ic = f"{quote_ident(cat)}.information_schema"
    s, t = sql_literal(sch), sql_literal(tbl)
    caveats: list[str] = []

    def q(sql: str, what: str) -> list[dict]:
        try:
            return executor.query(sql)
        except Exception as exc:
            caveats.append(f"{what} unavailable: {exc}")
            return []

    grants: dict[tuple, Grant] = {}
    for stmt in (f"SHOW GRANTS ON TABLE {quote_ident(cat)}.{quote_ident(sch)}.{quote_ident(tbl)}",
                 f"SHOW GRANTS ON SCHEMA {quote_ident(cat)}.{quote_ident(sch)}",
                 f"SHOW GRANTS ON CATALOG {quote_ident(cat)}"):
        for r in q(stmt, "grants (" + stmt.split(" ON ")[1].split(" ")[0].lower() + ")"):
            level = _LEVELS.get(str(_g(r, "objecttype", default="")).upper())
            principal = _g(r, "principal", "grantee")
            if level is None or principal is None:
                continue
            priv = _normalise_privilege(_g(r, "actiontype", "privilege_type", default=""), level)
            grants[(principal, priv, level)] = Grant(
                principal=principal, privilege=priv, level=level,
                inherited_from=None if level == "table" else level.upper())

    row_filters = [
        RowFilter(function=".".join(str(p) for p in (_g(r, "filter_catalog"), _g(r, "filter_schema"), _g(r, "filter_name")) if p),
                  target_columns=_split(_g(r, "filter_col_usage")))
        for r in q(f"SELECT * FROM {ic}.row_filters WHERE schema_name = {s} AND table_name = {t}", "row_filters")
    ]
    masks = [
        ColumnMask(column=str(_g(r, "column_name")),
                   function=".".join(str(p) for p in (_g(r, "mask_catalog"), _g(r, "mask_schema"), _g(r, "mask_name")) if p),
                   using_columns=_split(_g(r, "mask_col_usage")))
        for r in q(f"SELECT * FROM {ic}.column_masks WHERE schema_name = {s} AND table_name = {t}", "column_masks")
    ]

    tags = [Tag(name=str(_g(r, "tag_name")), value=_g(r, "tag_value"))
            for r in q(f"SELECT * FROM {ic}.table_tags WHERE schema_name = {s} AND table_name = {t}", "table_tags")]
    tags += [Tag(name=str(_g(r, "tag_name")), value=_g(r, "tag_value"), column=str(_g(r, "column_name")))
             for r in q(f"SELECT * FROM {ic}.column_tags WHERE schema_name = {s} AND table_name = {t}", "column_tags")]

    columns = [str(_g(r, "column_name")) for r in q(
        f"SELECT column_name FROM {ic}.columns WHERE table_schema = {s} AND table_name = {t} ORDER BY ordinal_position", "columns")]

    if not grants:
        caveats.append("no grants visible: either none exist or this principal cannot see them "
                       "(needs admin/owner, or READ METADATA/MANAGE plus usage on the parents)")
    return AccessSnapshot(table=table, grants=list(grants.values()), row_filters=row_filters, column_masks=masks,
                          tags=tags, columns=columns, caveats=caveats)
