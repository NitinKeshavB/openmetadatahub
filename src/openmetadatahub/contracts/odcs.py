"""A working subset of the Open Data Contract Standard (ODCS) v3.x.

Unknown fields are preserved (extra="allow") so a contract can round-trip without loss.
Covered: schema objects/properties, quality rules, slaProperties, roles, team, tags.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class QualityRule(_Base):
    type: str | None = None  # library | sql | text | custom
    metric: str | None = None  # nullValues, missingValues, invalidValues, duplicateValues, rowCount
    name: str | None = None
    description: str | None = None
    dimension: str | None = None
    severity: str | None = None
    unit: str | None = None  # rows | percent
    query: str | None = None
    arguments: dict[str, Any] | None = None
    mustBe: float | None = None
    mustNotBe: float | None = None
    mustBeGreaterThan: float | None = None
    mustBeGreaterOrEqualTo: float | None = None
    mustBeLessThan: float | None = None
    mustBeLessOrEqualTo: float | None = None
    mustBeBetween: list[float] | None = None
    mustNotBeBetween: list[float] | None = None


class SchemaProperty(_Base):
    name: str
    logicalType: str | None = None
    physicalType: str | None = None
    description: str | None = None
    required: bool = False
    unique: bool = False
    primaryKey: bool = False
    classification: str | None = None
    criticalDataElement: bool = False
    tags: list[str] = Field(default_factory=list)
    quality: list[QualityRule] = Field(default_factory=list)


class SchemaObject(_Base):
    name: str
    physicalName: str | None = None
    physicalType: str | None = None
    description: str | None = None
    properties: list[SchemaProperty] = Field(default_factory=list)
    quality: list[QualityRule] = Field(default_factory=list)


class SlaProperty(_Base):
    property: str
    value: float | str
    unit: str | None = None
    element: str | None = None  # "table.column"
    driver: str | None = None


class Role(_Base):
    role: str
    access: str | None = None  # read | write
    description: str | None = None


class DataContract(_Base):
    apiVersion: str = "v3.1.0"
    kind: str = "DataContract"
    id: str
    name: str | None = None
    version: str
    status: str = "draft"
    domain: str | None = None
    dataProduct: str | None = None
    description: Any = None
    tags: list[str] = Field(default_factory=list)
    schema_: list[SchemaObject] = Field(default_factory=list, alias="schema")
    slaProperties: list[SlaProperty] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)

    def qualified_table(self, obj: SchemaObject, namespace: str | None) -> str:
        """Resolve catalog.schema.table. physicalName may already be qualified; otherwise `namespace`
        (catalog.schema) supplies the prefix."""
        phys = obj.physicalName or obj.name
        parts = phys.split(".")
        if len(parts) == 3:
            return phys
        if len(parts) == 1 and namespace and len(namespace.split(".")) == 2:
            return f"{namespace}.{phys}"
        raise ValueError(
            f"cannot resolve {phys!r} to catalog.schema.table; pass namespace='catalog.schema' or use a qualified physicalName"
        )


def parse_contract(text: str) -> DataContract:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("contract must be a YAML mapping")
    return DataContract.model_validate(data)


def load_contract(source: str | Path) -> DataContract:
    """Load from a YAML file path or a YAML string."""
    if isinstance(source, Path) or ("\n" not in source and Path(source).exists()):
        return parse_contract(Path(source).read_text())
    return parse_contract(str(source))
