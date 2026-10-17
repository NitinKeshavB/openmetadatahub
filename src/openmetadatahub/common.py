"""Shared primitives: SQL execution boundary, identifier handling, severity."""

from __future__ import annotations

import re
from enum import Enum
from typing import (
    Any,
    Protocol,
)

Row = dict[str, Any]


class SqlExecutor(Protocol):
    """The only boundary to a warehouse. Implementations must be read-only unless stated."""

    def query(self, sql: str) -> list[Row]: ...


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}


def at_least(severity: Severity, gate: Severity) -> bool:
    return SEVERITY_RANK[severity] >= SEVERITY_RANK[gate]


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """Backtick-quote one Databricks identifier part, rejecting anything that could escape it."""
    if "`" in name or "\x00" in name or not name:
        raise ValueError(f"unsafe identifier: {name!r}")
    return f"`{name}`"


def quote_fqn(fqn: str) -> str:
    """Quote a dotted catalog.schema.table name. Each part is validated separately."""
    parts = fqn.split(".")
    if len(parts) != 3:
        raise ValueError(f"expected catalog.schema.table, got {fqn!r}")
    return ".".join(quote_ident(p) for p in parts)


def sql_literal(value: Any) -> str:
    """Render a Python value as a SQL literal. Strings are escaped; no raw interpolation elsewhere."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def is_simple_ident(name: str) -> bool:
    return bool(_IDENT.match(name))


class DatabricksSqlExecutor:
    """Read-only executor over databricks-sql-connector (optional dependency).

    Not exercised by the unit tests: it needs a live SQL warehouse. Use a service principal
    with SELECT on the target tables and USE on information_schema.
    """

    def __init__(self, server_hostname: str, http_path: str, access_token: str) -> None:
        try:
            from databricks import sql  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("install openmetadatahub[databricks] to use DatabricksSqlExecutor") from exc
        self._connect = lambda: sql.connect(
            server_hostname=server_hostname, http_path=http_path, access_token=access_token
        )

    def query(self, sql: str) -> list[Row]:  # pragma: no cover - needs a warehouse
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [c[0] for c in cur.description or []]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
