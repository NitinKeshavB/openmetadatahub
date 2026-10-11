"""AI governance model: assets, policies, framework controls and assessment results."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..common import Severity


class AssetKind(str, Enum):
    MODEL = "model"
    APPLICATION = "application"
    AGENT = "agent"
    MCP_SERVER = "mcp_server"
    GUARDRAIL = "guardrail"


class Stage(str, Enum):
    PROPOSAL = "proposal"
    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"
    DEPRECATED = "deprecated"
    UNAUTHORIZED = "unauthorized"  # discovered running without registration


class RiskClass(str, Enum):
    """Risk tiers modelled on the EU AI Act's structure. Classification is the owner's call; this
    library records it and applies the policy tied to it."""

    MINIMAL = "minimal"
    LIMITED = "limited"
    HIGH = "high"
    UNACCEPTABLE = "unacceptable"


class AIAsset(BaseModel):
    id: str
    name: str
    kind: AssetKind
    stage: Stage = Stage.DEVELOPMENT
    owner: str | None = None
    approved_by: str | None = None
    risk_class: RiskClass | None = None
    description: str | None = None
    intended_use: str | None = None
    limitations: str | None = None
    endpoint: str | None = None
    data_sources: list[str] = Field(default_factory=list, description="catalog.schema.table feeding training/RAG/features")
    data_contracts: list[str] = Field(default_factory=list, description="contract ids covering those sources")
    evaluations: dict[str, float] = Field(default_factory=dict,
                                          description="e.g. bias_score, hallucination_rate, pii_leakage_rate, accuracy")
    monitoring_enabled: bool = False
    guardrails_enabled: bool = False
    human_oversight: str | None = Field(default=None, description="How a human can intervene or override")
    labels: dict[str, str] = Field(default_factory=dict)
    source: str | None = Field(default=None, description="Where discovery found it, e.g. odh:ns/name")


class Comparator(str, Enum):
    LTE = "lte"
    GTE = "gte"


class EvaluationThreshold(BaseModel):
    metric: str
    comparator: Comparator
    value: float
    severity: Severity = Severity.ERROR


class AIPolicy(BaseModel):
    """Organisation policy applied to AI assets. Everything is optional; unset means not enforced."""

    name: str = "default"
    applies_to_risk: list[RiskClass] = Field(default_factory=lambda: [RiskClass.HIGH, RiskClass.LIMITED, RiskClass.MINIMAL])
    require_owner: bool = True
    require_approval_for_stages: list[Stage] = Field(default_factory=lambda: [Stage.PRODUCTION])
    require_risk_class_for_stages: list[Stage] = Field(default_factory=lambda: [Stage.STAGING, Stage.PRODUCTION])
    forbid_unauthorized: bool = True
    require_data_contract_for_sources: bool = True
    min_data_quality_score: float | None = 90.0
    require_active_contracts: bool = True
    forbid_access_findings_at_or_above: Severity | None = Severity.ERROR
    thresholds: list[EvaluationThreshold] = Field(default_factory=list)
    require_monitoring_for_risk: list[RiskClass] = Field(default_factory=lambda: [RiskClass.HIGH])
    require_human_oversight_for_risk: list[RiskClass] = Field(default_factory=lambda: [RiskClass.HIGH])
    require_model_documentation: bool = True


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    MANUAL = "manual"  # needs human evidence; never counted as a pass
    NOT_APPLICABLE = "not_applicable"


class Check(BaseModel):
    code: str
    status: CheckStatus
    severity: Severity
    message: str
    evidence: dict = Field(default_factory=dict)
    control_refs: list[str] = Field(default_factory=list, description="Framework control ids this check informs")


class Assessment(BaseModel):
    asset_id: str
    checks: list[Check]
    blocked: bool = Field(description="True if any failed check is error or critical")
    summary: dict[str, int]
