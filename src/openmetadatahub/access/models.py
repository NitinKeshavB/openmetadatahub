"""Access governance model for Unity Catalog."""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ..common import Severity


class Grant(BaseModel):
    principal: str
    privilege: str
    level: Literal["catalog", "schema", "table"]
    inherited_from: str | None = None


class RowFilter(BaseModel):
    function: str
    target_columns: list[str] = Field(default_factory=list)


class ColumnMask(BaseModel):
    column: str
    function: str
    using_columns: list[str] = Field(default_factory=list)


class Tag(BaseModel):
    name: str
    value: str | None = None
    column: str | None = None  # None for table-level tags


class AccessSnapshot(BaseModel):
    table: str
    grants: list[Grant]
    row_filters: list[RowFilter]
    column_masks: list[ColumnMask]
    tags: list[Tag]
    columns: list[str]
    caveats: list[str] = Field(default_factory=list)


class ClassificationRule(BaseModel):
    """If a column carries tag `tag` with one of `values`, then the table holding it must obey this rule."""

    tag: str
    values: list[str]
    require_mask: bool = True
    mask_function: str | None = Field(default=None, description="catalog.schema.fn used in remediation hints")
    allowed_principals: list[str] = Field(default_factory=list,
                                          description="Only these principals may SELECT an unmasked sensitive column")
    deny_principals: list[str] = Field(default_factory=list)


class AccessPolicy(BaseModel):
    classification_rules: list[ClassificationRule] = Field(default_factory=list)
    admin_principals: list[str] = Field(default_factory=list,
                                        description="Principals exempt from unexpected-grant findings")
    role_principals: dict[str, str] = Field(default_factory=dict,
                                            description="Maps contract role -> Databricks group; defaults to the role name")
    flag_direct_user_grants: bool = True

    @classmethod
    def from_yaml(cls, text: str) -> "AccessPolicy":
        return cls.model_validate(yaml.safe_load(text) or {})


class Finding(BaseModel):
    table: str
    code: str
    severity: Severity
    message: str
    principal: str | None = None
    column: str | None = None
    remedy: str | None = Field(default=None, description="Dry-run SQL. Never executed by this library.")
