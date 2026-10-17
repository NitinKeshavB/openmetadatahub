from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from uuid import uuid4

from ..common import Severity


class ConfigError(RuntimeError):
    """The job cannot produce trustworthy evidence (missing contracts, bad path). Never treated as a pass."""


class EnforcementFailed(RuntimeError):
    """Raised at the end of a run in gate mode so the Databricks task fails and downstream work stops."""


@dataclass
class JobConfig:
    contracts_path: str
    results_catalog: str | None = None
    results_schema: str = "governance"
    namespace: str | None = None  # catalog.schema for unqualified physicalNames
    enforcement: str = "gate"  # gate: fail the task on violations; report: record only
    gate_severity: Severity = Severity.ERROR
    enforced_statuses: tuple[str, ...] = ("active",)  # other statuses are recorded but never block
    contract_ids: tuple[str, ...] = ()  # empty = every contract in the folder
    access_policy_path: str | None = None
    ai_manifests_path: str | None = None
    ai_policy_path: str | None = None
    max_evidence_age_hours: int = 48  # older quality/access evidence counts as missing
    fail_on_incomplete_audit: bool = True  # an access audit that could not see grants is not a pass
    run_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.enforcement not in ("gate", "report"):
            raise ConfigError(f"enforcement must be 'gate' or 'report', got {self.enforcement!r}")
