"""Console entry points used by Databricks python_wheel_task. Parameters arrive as CLI arguments."""

from __future__ import annotations

import argparse

from ..common import Severity
from .config import JobConfig
from .runner import Outcome, assess_ai, audit_access, enforce_contracts
from .spark import get_spark


def _csv(v: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (v or "").split(",") if p.strip())


def _parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument("--contracts-path", required=True, help="folder (or file) of ODCS YAML, e.g. a Unity Catalog volume")
    p.add_argument("--results-catalog", default="", help="catalog for result tables; empty = hive_metastore-style schema.table")
    p.add_argument("--results-schema", default="governance")
    p.add_argument("--namespace", default="", help="catalog.schema for unqualified physicalNames")
    p.add_argument("--enforcement", default="gate", choices=["gate", "report"])
    p.add_argument("--gate-severity", default="error", choices=[s.value for s in Severity])
    p.add_argument("--enforced-statuses", default="active")
    p.add_argument("--contract-ids", default="", help="comma-separated; empty = all")
    p.add_argument("--access-policy", default="")
    p.add_argument("--ai-manifests", default="")
    p.add_argument("--ai-policy", default="")
    p.add_argument("--max-evidence-age-hours", type=int, default=48)
    p.add_argument("--run-id", default="", help="pass {{job.run_id}}")
    return p


def _config(a: argparse.Namespace) -> JobConfig:
    kw = dict(
        contracts_path=a.contracts_path, results_catalog=a.results_catalog or None, results_schema=a.results_schema,
        namespace=a.namespace or None, enforcement=a.enforcement, gate_severity=Severity(a.gate_severity),
        enforced_statuses=_csv(a.enforced_statuses) or ("active",), contract_ids=_csv(a.contract_ids),
        access_policy_path=a.access_policy or None, ai_manifests_path=a.ai_manifests or None,
        ai_policy_path=a.ai_policy or None, max_evidence_age_hours=a.max_evidence_age_hours,
    )
    if a.run_id:
        kw["run_id"] = a.run_id
    return JobConfig(**kw)


def _report(what: str, out: Outcome) -> None:
    for subject, decision in sorted(out.decisions.items()):
        print(f"[{what}] {decision:8s} {subject}")


def enforce_contracts_main(argv: list[str] | None = None) -> None:
    cfg = _config(_parser("omh-enforce-contracts").parse_args(argv))
    _report("contracts", enforce_contracts(get_spark(), cfg))


def audit_access_main(argv: list[str] | None = None) -> None:
    cfg = _config(_parser("omh-audit-access").parse_args(argv))
    _report("access", audit_access(get_spark(), cfg))


def assess_ai_main(argv: list[str] | None = None) -> None:
    cfg = _config(_parser("omh-assess-ai").parse_args(argv))
    _report("ai", assess_ai(get_spark(), cfg))
