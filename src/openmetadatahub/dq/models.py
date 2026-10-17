"""Data quality domain model: dimensions, test cases and results."""

from __future__ import annotations

from datetime import (
    datetime,
    timezone,
)
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    Field,
)

from ..common import Severity


class Dimension(str, Enum):
    COMPLETENESS = "completeness"
    UNIQUENESS = "uniqueness"
    VALIDITY = "validity"
    ACCURACY = "accuracy"
    CONSISTENCY = "consistency"
    INTEGRITY = "integrity"
    TIMELINESS = "timeliness"
    VOLUME = "volume"
    CUSTOM = "custom"


class Scope(str, Enum):
    TABLE = "table"
    COLUMN = "column"


class Status(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"


class TestCase(BaseModel):
    """One executable assertion against a table or column.

    `metric` names an entry in the catalog; `params` are validated by that entry.
    Row-wise metrics tolerate `max_failed_rows` if set, otherwise `max_failed_pct`.
    Scalar metrics compare against params: equals, ne, min, max, lt, gt, outside=[lo, hi].
    """

    __test__ = False  # not a pytest class

    name: str
    table: str = Field(description="catalog.schema.table")
    column: str | None = None
    metric: str
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Severity = Severity.ERROR
    max_failed_pct: float = 0.0
    max_failed_rows: int | None = None
    where: str | None = Field(
        default=None,
        description="Trusted partition filter appended to the query, e.g. ingest_date >= '2026-01-01'.",
    )
    source: str | None = Field(default=None, description="Origin, e.g. a data contract id.")


class TestResult(BaseModel):
    __test__ = False

    case: TestCase
    dimension: Dimension
    status: Status
    observed: float | None = None
    total_rows: int | None = None
    failed_rows: int | None = None
    failed_pct: float | None = None
    message: str = ""
    sql: str = ""
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QualityReport(BaseModel):
    table: str
    score: float = Field(description="Severity-weighted pass rate, 0-100")
    by_dimension: dict[str, float]
    passed: int
    failed: int
    aborted: int
    results: list[TestResult]
