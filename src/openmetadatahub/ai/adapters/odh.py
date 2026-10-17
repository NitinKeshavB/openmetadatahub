"""Discover AI assets from Open Data Hub / OpenShift AI Kubernetes manifests.

Works on exported YAML (kubectl get ... -o yaml) so it needs no cluster credentials. Recognised kinds:
  serving.kserve.io  InferenceService          -> model
  trustyai.opendatahub.io TrustyAIService      -> marks models in the namespace as monitored
  trustyai.opendatahub.io LMEvalJob            -> records that an evaluation was run for a model
  trustyai.opendatahub.io NemoGuardrails       -> marks models in the namespace as guarded

Governance metadata comes from annotations on the InferenceService, using the prefix
`openmetadatahub.io/`: owner, approved-by, risk-class, stage, intended-use, limitations,
human-oversight, data-sources (comma-separated catalog.schema.table), data-contracts.
An InferenceService with no such annotations is still returned, marked UNAUTHORIZED, because a model
serving traffic with no governance record is exactly what an AI inventory exists to surface.
"""

from __future__ import annotations

from typing import Iterable

import yaml

from ..models import (
    AIAsset,
    AssetKind,
    RiskClass,
    Stage,
)

PREFIX = "openmetadatahub.io/"


def _csv(v: str | None) -> list[str]:
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def _enum(cls, value: str | None):
    try:
        return cls(value.lower()) if value else None
    except ValueError:
        return None


def discover(manifests: str | Iterable[dict]) -> list[AIAsset]:
    docs = list(yaml.safe_load_all(manifests)) if isinstance(manifests, str) else list(manifests)
    items: list[dict] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        items += d["items"] if d.get("kind") == "List" and isinstance(d.get("items"), list) else [d]

    monitored_ns = {i["metadata"].get("namespace", "default") for i in items if i.get("kind") == "TrustyAIService"}
    guarded_ns = {i["metadata"].get("namespace", "default") for i in items if i.get("kind") == "NemoGuardrails"}
    evaluated: dict[tuple[str, str], list[str]] = {}
    for i in items:
        if i.get("kind") == "LMEvalJob":
            ns = i["metadata"].get("namespace", "default")
            spec = i.get("spec", {})
            evaluated.setdefault((ns, spec.get("model", "")), []).extend(spec.get("taskList", {}).get("taskNames", []))

    assets: list[AIAsset] = []
    for i in items:
        if i.get("kind") != "InferenceService":
            continue
        meta = i.get("metadata", {})
        ns, name = meta.get("namespace", "default"), meta["name"]
        ann = {k[len(PREFIX) :]: v for k, v in (meta.get("annotations") or {}).items() if k.startswith(PREFIX)}
        governed = bool(ann)
        stage = _enum(Stage, ann.get("stage")) or (Stage.DEVELOPMENT if governed else Stage.UNAUTHORIZED)
        status_url = (i.get("status") or {}).get("url")
        assets.append(
            AIAsset(
                id=f"odh:{ns}/{name}",
                name=name,
                kind=AssetKind.MODEL,
                stage=stage,
                owner=ann.get("owner"),
                approved_by=ann.get("approved-by"),
                risk_class=_enum(RiskClass, ann.get("risk-class")),
                intended_use=ann.get("intended-use"),
                limitations=ann.get("limitations"),
                human_oversight=ann.get("human-oversight"),
                endpoint=status_url,
                data_sources=_csv(ann.get("data-sources")),
                data_contracts=_csv(ann.get("data-contracts")),
                monitoring_enabled=ns in monitored_ns,
                guardrails_enabled=ns in guarded_ns,
                labels=(
                    {"evaluation_tasks": ",".join(sorted(set(evaluated.get((ns, name), []))))}
                    if (ns, name) in evaluated
                    else {}
                ),
                source=f"odh:{ns}/{name}",
            )
        )
    return assets
