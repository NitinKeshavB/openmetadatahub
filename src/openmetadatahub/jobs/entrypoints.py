"""Console entry points used by Databricks python_wheel_task. Parameters arrive as CLI arguments."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Callable

from ..common import Severity
from ..runlog import RunEventBuffer, configure_run_logging, logger
from .config import JobConfig
from .runner import Outcome, assess_ai, audit_access, enforce_contracts
from .spark import get_spark
from .store import ResultStore


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
        log = logger.warning if decision in ("blocked", "invalid", "warn") else logger.info
        log("[{}] {} {}", what, decision, subject, subject=subject, decision=decision)


def _store_events(spark: Any, cfg: JobConfig, events: RunEventBuffer) -> None:
    rows = [
        dict(run_id=e.run_id, seq=e.seq, level=e.level, message=e.message[:4000], logged_at=e.logged_at,
             context=json.dumps(dict(e.context), default=str)[:4000])
        for e in events.events
    ]
    store = ResultStore(spark, cfg.results_catalog, cfg.results_schema)
    store.ensure()
    store.append("run_events", rows)


def _run(job: str, what: str, argv: list[str] | None, action: Callable[[Any, JobConfig], Outcome]) -> None:
    """Configure run logging, run one governance job, report its decisions and keep its log events.

    Events are stored even when the job raises (a closed gate fails the task last, after everything is recorded)."""
    cfg = _config(_parser(job).parse_args(argv))
    events = configure_run_logging({"run_id": cfg.run_id, "job": job, "target": os.environ.get("BUNDLE_TARGET", ""),
                                    "git_sha": os.environ.get("GIT_COMMIT", "")})
    spark = get_spark()
    try:
        out = action(spark, cfg)
        _report(what, out)
    except Exception as exc:
        logger.opt(exception=exc).error("{} failed: {}", job, exc)
        raise
    finally:
        _store_events(spark, cfg, events)


def enforce_contracts_main(argv: list[str] | None = None) -> None:
    _run("omh-enforce-contracts", "contracts", argv, enforce_contracts)


def audit_access_main(argv: list[str] | None = None) -> None:
    _run("omh-audit-access", "access", argv, audit_access)


def assess_ai_main(argv: list[str] | None = None) -> None:
    _run("omh-assess-ai", "ai", argv, assess_ai)
