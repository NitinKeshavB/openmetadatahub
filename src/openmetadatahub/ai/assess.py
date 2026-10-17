"""Assess an AI asset against policy, using the data estate's own quality, contract and access evidence.

This is the link between AI governance and data governance: an asset is only as governed as the
tables it learns from or retrieves from.
"""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)

from ..access.models import Finding
from ..common import Severity
from ..contracts.odcs import DataContract
from .models import (
    AIAsset,
    AIPolicy,
    Assessment,
    Check,
    CheckStatus,
    Comparator,
    RiskClass,
    Stage,
)

_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}


@dataclass
class Evidence:
    """What the data estate currently says about the tables an asset depends on."""

    quality_scores: dict[str, float] = field(default_factory=dict)  # table -> 0..100
    contracts: dict[str, DataContract] = field(default_factory=dict)  # contract id -> contract
    table_contracts: dict[str, str] = field(default_factory=dict)  # table -> contract id
    access_findings: dict[str, list[Finding]] = field(default_factory=dict)  # table -> findings


def _chk(code, status, sev, msg, refs=(), **ev) -> Check:
    return Check(code=code, status=status, severity=sev, message=msg, evidence=ev, control_refs=list(refs))


def assess(asset: AIAsset, policy: AIPolicy, ev: Evidence | None = None) -> Assessment:
    ev = ev or Evidence()
    out: list[Check] = []
    risk = asset.risk_class

    if risk is not None and risk not in policy.applies_to_risk and risk != RiskClass.UNACCEPTABLE:
        return Assessment(asset_id=asset.id, checks=[], blocked=False, summary={"not_applicable": 1})

    if risk == RiskClass.UNACCEPTABLE:
        out.append(
            _chk(
                "prohibited-risk-class",
                CheckStatus.FAIL,
                Severity.CRITICAL,
                "asset is classified as unacceptable risk and must not be deployed",
                ["EUAIA-5"],
            )
        )

    if policy.forbid_unauthorized and asset.stage == Stage.UNAUTHORIZED:
        out.append(
            _chk(
                "unregistered-asset",
                CheckStatus.FAIL,
                Severity.CRITICAL,
                "asset was discovered running without registration or approval",
                ["NIST-GOVERN"],
            )
        )

    if policy.require_owner:
        ok = bool(asset.owner)
        out.append(
            _chk(
                "owner-assigned",
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                Severity.ERROR,
                "owner assigned" if ok else "no accountable owner",
                ["NIST-GOVERN"],
            )
        )

    if asset.stage in policy.require_approval_for_stages:
        ok = bool(asset.approved_by)
        out.append(
            _chk(
                "approval-recorded",
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                Severity.ERROR,
                f"approved by {asset.approved_by}" if ok else f"{asset.stage.value} requires a recorded approval",
                ["NIST-GOVERN"],
            )
        )

    if asset.stage in policy.require_risk_class_for_stages:
        ok = risk is not None
        out.append(
            _chk(
                "risk-class-set",
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                Severity.ERROR,
                f"risk class {risk.value}" if ok else "risk class not assessed",
                ["EUAIA-9", "NIST-MAP"],
            )
        )

    if policy.require_model_documentation:
        missing = [f for f in ("intended_use", "limitations") if not getattr(asset, f)]
        out.append(
            _chk(
                "documentation",
                CheckStatus.PASS if not missing else CheckStatus.FAIL,
                Severity.WARNING,
                "intended use and limitations documented" if not missing else f"missing: {', '.join(missing)}",
                ["EUAIA-11", "EUAIA-13", "NIST-MAP"],
            )
        )

    out += _data_checks(asset, policy, ev)

    for th in policy.thresholds:
        val = asset.evaluations.get(th.metric)
        if val is None:
            out.append(
                _chk(
                    f"eval:{th.metric}",
                    CheckStatus.FAIL,
                    th.severity,
                    f"no evaluation result recorded for {th.metric}",
                    ["EUAIA-15", "NIST-MEASURE"],
                )
            )
            continue
        ok = val <= th.value if th.comparator == Comparator.LTE else val >= th.value
        out.append(
            _chk(
                f"eval:{th.metric}",
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                th.severity,
                f"{th.metric}={val:g} ({'meets' if ok else 'violates'} {th.comparator.value} {th.value:g})",
                ["EUAIA-15", "NIST-MEASURE"],
                observed=val,
            )
        )

    if risk in policy.require_monitoring_for_risk:
        out.append(
            _chk(
                "monitoring-enabled",
                CheckStatus.PASS if asset.monitoring_enabled else CheckStatus.FAIL,
                Severity.ERROR,
                "monitoring enabled" if asset.monitoring_enabled else "high-risk asset has no monitoring",
                ["EUAIA-12", "NIST-MANAGE"],
            )
        )
    if risk in policy.require_human_oversight_for_risk:
        ok = bool(asset.human_oversight)
        out.append(
            _chk(
                "human-oversight",
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                Severity.ERROR,
                "human oversight described" if ok else "no human oversight mechanism recorded",
                ["EUAIA-14", "NIST-GOVERN"],
            )
        )

    summary: dict[str, int] = {}
    for c in out:
        summary[c.status.value] = summary.get(c.status.value, 0) + 1
    blocked = any(c.status == CheckStatus.FAIL and _RANK[c.severity] >= _RANK[Severity.ERROR] for c in out)
    return Assessment(asset_id=asset.id, checks=out, blocked=blocked, summary=summary)


def _data_checks(asset: AIAsset, policy: AIPolicy, ev: Evidence) -> list[Check]:
    out: list[Check] = []
    if not asset.data_sources:
        if asset.kind.value in ("model", "application", "agent"):
            out.append(
                _chk(
                    "data-sources-declared",
                    CheckStatus.MANUAL,
                    Severity.WARNING,
                    "no data sources declared; data governance cannot be verified",
                    ["EUAIA-10"],
                )
            )
        return out

    for table in asset.data_sources:
        cid = ev.table_contracts.get(table) or next((c for c in asset.data_contracts if c in ev.contracts), None)
        if policy.require_data_contract_for_sources:
            out.append(
                _chk(
                    f"contract:{table}",
                    CheckStatus.PASS if cid else CheckStatus.FAIL,
                    Severity.ERROR,
                    f"covered by contract {cid}" if cid else "no data contract covers this source",
                    ["EUAIA-10", "NIST-MAP"],
                    table=table,
                )
            )
        if cid and policy.require_active_contracts and cid in ev.contracts:
            status = ev.contracts[cid].status
            ok = status.lower() == "active"
            out.append(
                _chk(
                    f"contract-active:{table}",
                    CheckStatus.PASS if ok else CheckStatus.FAIL,
                    Severity.WARNING,
                    f"contract {cid} status is {status}",
                    ["EUAIA-10"],
                    table=table,
                )
            )
        if policy.min_data_quality_score is not None:
            score = ev.quality_scores.get(table)
            if score is None:
                out.append(
                    _chk(
                        f"quality:{table}",
                        CheckStatus.FAIL,
                        Severity.ERROR,
                        "no data quality result for this source",
                        ["EUAIA-10", "NIST-MEASURE"],
                        table=table,
                    )
                )
            else:
                ok = score >= policy.min_data_quality_score
                out.append(
                    _chk(
                        f"quality:{table}",
                        CheckStatus.PASS if ok else CheckStatus.FAIL,
                        Severity.ERROR,
                        f"quality score {score:g} vs minimum {policy.min_data_quality_score:g}",
                        ["EUAIA-10", "NIST-MEASURE"],
                        table=table,
                        score=score,
                    )
                )
        gate = policy.forbid_access_findings_at_or_above
        if gate is not None:
            bad = [f for f in ev.access_findings.get(table, []) if _RANK[f.severity] >= _RANK[gate]]
            out.append(
                _chk(
                    f"access:{table}",
                    CheckStatus.PASS if not bad else CheckStatus.FAIL,
                    Severity.ERROR,
                    (
                        "no blocking access findings"
                        if not bad
                        else f"{len(bad)} access finding(s) at {gate.value} or above: "
                        + ", ".join(sorted({f.code for f in bad}))
                    ),
                    ["EUAIA-10", "NIST-MANAGE"],
                    table=table,
                )
            )
    return out
