"""The three governance jobs. Each takes a Spark session (the engine), reads evidence with read-only SQL,
records everything to Delta, and raises EnforcementFailed at the end if the gate is closed.

Design rules:
  * Record first, fail last: every contract is checked and persisted even when an earlier one is blocked.
  * Missing evidence is never a pass: no contracts found, an unparseable contract, an aborted test, or an
    audit that could not see grants all block.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..access import AccessPolicy, Finding, evaluate, snapshot_table
from ..ai import AIPolicy, Evidence, assess, control_coverage, discover_odh
from ..common import Severity, SqlExecutor, at_least
from ..contracts import DataContract, contract_to_tests, diff_contracts, parse_contract, validate_schema
from ..dq import Status, run_cases, summarise
from .config import ConfigError, EnforcementFailed, JobConfig
from .spark import SparkSqlExecutor
from .store import ResultStore, now_utc


@dataclass
class Outcome:
    decisions: dict[str, str] = field(default_factory=dict)  # subject -> pass | warn | blocked | invalid

    @property
    def blocked(self) -> list[str]:
        return sorted(k for k, v in self.decisions.items() if v in ("blocked", "invalid"))

    def raise_if_blocked(self, cfg: JobConfig, what: str) -> None:
        if cfg.enforcement == "gate" and self.blocked:
            raise EnforcementFailed(f"{what} blocked: {', '.join(self.blocked)} (run {cfg.run_id})")


def _files(path: str) -> list[Path]:
    p = Path(path)
    if p.is_file():
        return [p]
    if not p.is_dir():
        raise ConfigError(f"path not found: {path}")
    return sorted(f for f in p.iterdir() if f.suffix.lower() in (".yaml", ".yml") and f.is_file())


def load_contracts(cfg: JobConfig) -> tuple[list[tuple[Path, str, DataContract]], list[tuple[Path, str]]]:
    """Returns (valid contracts as (path, text, contract), invalid files as (path, error))."""
    good, bad = [], []
    for f in _files(cfg.contracts_path):
        text = f.read_text()
        try:
            c = parse_contract(text)
        except Exception as exc:
            bad.append((f, f"{type(exc).__name__}: {str(exc)[:300]}"))
            continue
        if cfg.contract_ids and c.id not in cfg.contract_ids:
            continue
        good.append((f, text, c))
    found = {c.id for _, _, c in good}
    if not good and not bad:
        raise ConfigError(f"no contracts found in {cfg.contracts_path}"
                          + (f" matching {list(cfg.contract_ids)}" if cfg.contract_ids else ""))
    missing = set(cfg.contract_ids) - found
    if missing and not bad:
        raise ConfigError(f"contract ids not found: {sorted(missing)}")
    return good, bad


# ------------------------------------------------------------------ contract enforcement

def _track_version(store: ResultStore, c: DataContract, text: str, cfg: JobConfig, ts) -> tuple[bool, str]:
    """Judge the contract's current text against its last compliant version. The verdict is stored on the
    version row, so a non-compliant change keeps blocking every run until a compliant version replaces it."""
    sha = hashlib.sha256(text.encode()).hexdigest()
    latest = store.latest_contract_version(c.id)
    if latest and latest["sha256"] == sha:
        return bool(latest["version_ok"]), latest["diff_summary"] or ""
    base = store.baseline_contract_version(c.id)
    ok, summary = True, "first version seen" if base is None else ""
    if base is not None:
        diff = diff_contracts(parse_contract(base["yaml"]), c)
        ok = diff.version_ok
        summary = (f"vs {base['version']}: required {diff.required_bump} bump, declared {diff.declared_bump}; "
                   + "; ".join(f"{ch.kind}:{ch.code}:{ch.path}" for ch in diff.changes[:10]))
    store.append("contract_versions", [dict(
        contract_id=c.id, version=c.version, sha256=sha, yaml=text, first_seen_run_id=cfg.run_id,
        first_seen_ts=ts, version_ok=ok, diff_summary=summary[:2000])])
    return ok, summary


def enforce_contracts(spark: Any, cfg: JobConfig, executor: SqlExecutor | None = None) -> Outcome:
    ex = executor or SparkSqlExecutor(spark)
    store = ResultStore(spark, cfg.results_catalog, cfg.results_schema)
    store.ensure()
    ts = now_utc()
    good, bad = load_contracts(cfg)
    out = Outcome()
    runs, results_rows, scores, issue_rows = [], [], [], []

    for path, why in bad:
        out.decisions[path.name] = "invalid"
        runs.append(dict(run_id=cfg.run_id, run_ts=ts, contract_id=path.name, contract_status="unknown", blocking=1,
                         decision="invalid", detail=json.dumps({"error": why})))

    for _, text, c in good:
        try:
            tests = contract_to_tests(c, cfg.namespace)
            version_ok, version_note = _track_version(store, c, text, cfg, ts)
            results = run_cases(tests, ex)
            issues = validate_schema(c, ex, cfg.namespace)
        except ValueError as exc:  # e.g. a table name that cannot be resolved
            out.decisions[c.id] = "invalid"
            runs.append(dict(run_id=cfg.run_id, run_ts=ts, contract_id=c.id, contract_version=c.version,
                             contract_status=c.status, blocking=1, decision="invalid", detail=json.dumps({"error": str(exc)})))
            continue

        by_table: dict[str, list] = {}
        for r in results:
            by_table.setdefault(r.case.table, []).append(r)
            results_rows.append(dict(
                run_id=cfg.run_id, run_ts=ts, contract_id=c.id, test_name=r.case.name, table_name=r.case.table,
                column_name=r.case.column, metric=r.case.metric, dimension=r.dimension.value,
                severity=r.case.severity.value, status=r.status.value, observed=r.observed, total_rows=r.total_rows,
                failed_rows=r.failed_rows, failed_pct=r.failed_pct, message=r.message[:1000], sql_text=r.sql))
        for table, rs in by_table.items():
            rep = summarise(table, rs)
            scores.append(dict(run_id=cfg.run_id, run_ts=ts, table_name=table, score=rep.score,
                               by_dimension=json.dumps(rep.by_dimension), passed=rep.passed, failed=rep.failed, aborted=rep.aborted))
        for i in issues:
            issue_rows.append(dict(run_id=cfg.run_id, run_ts=ts, contract_id=c.id, table_name=i.table, column_name=i.column,
                                   code=i.code, severity=i.severity.value, message=i.message))

        bad_tests = [r for r in results if r.status != Status.SUCCESS and at_least(r.case.severity, cfg.gate_severity)]
        bad_issues = [i for i in issues if at_least(i.severity, cfg.gate_severity)]
        blocking = len(bad_tests) + len(bad_issues) + (0 if version_ok else 1)
        enforced = c.status.lower() in cfg.enforced_statuses
        decision = "pass" if blocking == 0 else ("blocked" if enforced else "warn")
        out.decisions[c.id] = decision
        runs.append(dict(
            run_id=cfg.run_id, run_ts=ts, contract_id=c.id, contract_version=c.version, contract_status=c.status,
            tables_checked=len(by_table), tests_total=len(results),
            tests_failed=sum(r.status == Status.FAILED for r in results), tests_aborted=sum(r.status == Status.ABORTED for r in results),
            schema_issues=len(issues), version_ok=version_ok, blocking=blocking, decision=decision,
            detail=json.dumps({"failing": [r.case.name for r in bad_tests][:25], "schema": [f"{i.table}.{i.column}:{i.code}" for i in bad_issues][:25],
                               "version": None if version_ok else version_note})[:4000]))

    store.append("dq_results", results_rows)
    store.append("quality_scores", scores)
    store.append("schema_issues", issue_rows)
    store.append("contract_runs", runs)
    out.raise_if_blocked(cfg, "contract enforcement")
    return out


# ------------------------------------------------------------------ access audit

def audit_access(spark: Any, cfg: JobConfig, executor: SqlExecutor | None = None) -> Outcome:
    ex = executor or SparkSqlExecutor(spark)
    store = ResultStore(spark, cfg.results_catalog, cfg.results_schema)
    store.ensure()
    ts = now_utc()
    policy = AccessPolicy.from_yaml(Path(cfg.access_policy_path).read_text()) if cfg.access_policy_path else AccessPolicy()
    good, bad = load_contracts(cfg)
    out = Outcome({p.name: "invalid" for p, _ in bad})

    tables: dict[str, DataContract] = {}
    for _, _, c in good:
        for obj in c.schema_:
            try:
                tables.setdefault(c.qualified_table(obj, cfg.namespace), c)
            except ValueError:
                out.decisions[c.id] = "invalid"

    audits, finding_rows = [], []
    for table, c in sorted(tables.items()):
        snap = snapshot_table(ex, table)
        findings = evaluate(snap, policy, c)
        blocking = [f for f in findings if at_least(f.severity, cfg.gate_severity)
                    or (cfg.fail_on_incomplete_audit and f.code == "snapshot-caveat")]
        audits.append(dict(run_id=cfg.run_id, run_ts=ts, table_name=table, contract_id=c.id, findings=len(findings),
                           blocking=len(blocking), caveats=json.dumps(snap.caveats)))
        for f in findings:
            finding_rows.append(dict(run_id=cfg.run_id, run_ts=ts, table_name=table, code=f.code, severity=f.severity.value,
                                     principal=f.principal, column_name=f.column, message=f.message[:1000], remedy=f.remedy))
        enforced = c.status.lower() in cfg.enforced_statuses
        out.decisions[table] = "pass" if not blocking else ("blocked" if enforced else "warn")

    store.append("access_audits", audits)
    store.append("access_findings", finding_rows)
    out.raise_if_blocked(cfg, "access audit")
    return out


# ------------------------------------------------------------------ AI governance

def assess_ai(spark: Any, cfg: JobConfig) -> Outcome:
    if not cfg.ai_manifests_path:
        raise ConfigError("ai_manifests_path is required")
    store = ResultStore(spark, cfg.results_catalog, cfg.results_schema)
    store.ensure()
    ts = now_utc()
    manifests = "\n---\n".join(f.read_text() for f in _files(cfg.ai_manifests_path))
    assets = discover_odh(manifests)
    policy = AIPolicy.model_validate(yaml.safe_load(Path(cfg.ai_policy_path).read_text()) or {}) if cfg.ai_policy_path else AIPolicy()

    good, _ = load_contracts(JobConfig(**{**cfg.__dict__, "contract_ids": ()}))
    contracts = {c.id: c for _, _, c in good}
    table_contracts = {}
    for _, _, c in good:
        for obj in c.schema_:
            try:
                table_contracts.setdefault(c.qualified_table(obj, cfg.namespace), c.id)
            except ValueError:
                pass

    raw = store.latest_access_findings(cfg.max_evidence_age_hours)
    ev = Evidence(
        quality_scores=store.latest_quality_scores(cfg.max_evidence_age_hours),
        contracts=contracts, table_contracts=table_contracts,
        access_findings={t: [Finding(table=t, code=r["code"], severity=Severity(r["severity"]), message=r["message"],
                                     principal=r.get("principal"), column=r.get("column_name")) for r in rows]
                         for t, rows in raw.items()})

    out, rows = Outcome(), []
    for a in assets:
        res = assess(a, policy, ev)
        out.decisions[a.id] = "blocked" if res.blocked else "pass"
        rows.append(dict(run_id=cfg.run_id, run_ts=ts, asset_id=a.id, asset_name=a.name, stage=a.stage.value,
                         risk_class=a.risk_class.value if a.risk_class else None, blocked=res.blocked,
                         checks=json.dumps([c.model_dump(mode="json") for c in res.checks]),
                         control_coverage=json.dumps(control_coverage(res))))
    store.append("ai_assessments", rows)
    out.raise_if_blocked(cfg, "AI governance")
    return out
