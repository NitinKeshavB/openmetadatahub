"""Detect breaking changes between two versions of a data contract and check the version bump."""

from __future__ import annotations

from pydantic import BaseModel

from .odcs import DataContract


class Change(BaseModel):
    kind: str  # breaking | additive | tightening | note
    code: str
    path: str
    message: str


class Diff(BaseModel):
    changes: list[Change]
    required_bump: str  # major | minor | patch | none
    declared_bump: str
    version_ok: bool


def _parse(v: str) -> tuple[int, int, int]:
    nums = [int(p) for p in v.split("-")[0].split(".")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))  # type: ignore[return-value]


def _declared_bump(old: str, new: str) -> str:
    o, n = _parse(old), _parse(new)
    if n[0] > o[0]:
        return "major"
    if n[0] == o[0] and n[1] > o[1]:
        return "minor"
    if n[:2] == o[:2] and n[2] > o[2]:
        return "patch"
    return "none"


def diff_contracts(old: DataContract, new: DataContract) -> Diff:
    ch: list[Change] = []
    old_objs = {o.name: o for o in old.schema_}
    new_objs = {o.name: o for o in new.schema_}

    for name in old_objs.keys() - new_objs.keys():
        ch.append(Change(kind="breaking", code="object-removed", path=name, message="schema object removed"))
    for name in new_objs.keys() - old_objs.keys():
        ch.append(Change(kind="additive", code="object-added", path=name, message="schema object added"))

    for name in old_objs.keys() & new_objs.keys():
        op = {p.name: p for p in old_objs[name].properties}
        np_ = {p.name: p for p in new_objs[name].properties}
        for col in op.keys() - np_.keys():
            ch.append(Change(kind="breaking", code="column-removed", path=f"{name}.{col}", message="column removed"))
        for col in np_.keys() - op.keys():
            kind = "tightening" if np_[col].required else "additive"
            ch.append(
                Change(
                    kind=kind,
                    code="column-added",
                    path=f"{name}.{col}",
                    message="required column added" if np_[col].required else "column added",
                )
            )
        for col in op.keys() & np_.keys():
            a, b, path = op[col], np_[col], f"{name}.{col}"
            if (a.physicalType or a.logicalType) != (b.physicalType or b.logicalType):
                ch.append(
                    Change(
                        kind="breaking",
                        code="type-changed",
                        path=path,
                        message=f"{a.physicalType or a.logicalType} -> {b.physicalType or b.logicalType}",
                    )
                )
            if a.required and not b.required:
                ch.append(
                    Change(
                        kind="breaking", code="required-relaxed", path=path, message="consumers may now receive nulls"
                    )
                )
            if not a.required and b.required:
                ch.append(
                    Change(
                        kind="tightening",
                        code="required-added",
                        path=path,
                        message="producers must now always populate this column",
                    )
                )
            if a.unique != b.unique and a.unique:
                ch.append(
                    Change(kind="breaking", code="unique-dropped", path=path, message="uniqueness guarantee removed")
                )
            if a.classification != b.classification:
                ch.append(
                    Change(
                        kind="note",
                        code="classification-changed",
                        path=path,
                        message=f"{a.classification} -> {b.classification}; review access policy",
                    )
                )

    old_sla = {(s.property, s.element): s for s in old.slaProperties}
    for s in new.slaProperties:
        prev = old_sla.get((s.property, s.element))
        if prev is None:
            ch.append(Change(kind="additive", code="sla-added", path=f"{s.property}:{s.element}", message="SLA added"))
        elif prev.value != s.value or prev.unit != s.unit:
            ch.append(
                Change(
                    kind="breaking",
                    code="sla-changed",
                    path=f"{s.property}:{s.element}",
                    message=(
                        f"{prev.value}{prev.unit or ''} -> {s.value}{s.unit or ''}; review whether consumers are "
                        "weakened"
                    ),
                )
            )
    for key in old_sla.keys() - {(s.property, s.element) for s in new.slaProperties}:
        ch.append(Change(kind="breaking", code="sla-removed", path=f"{key[0]}:{key[1]}", message="SLA removed"))

    kinds = {c.kind for c in ch}
    required = (
        "major"
        if "breaking" in kinds
        else "minor" if kinds & {"additive", "tightening"} else "patch" if ch else "none"
    )
    declared = _declared_bump(old.version, new.version)
    order = {"none": 0, "patch": 1, "minor": 2, "major": 3}
    return Diff(
        changes=ch, required_bump=required, declared_bump=declared, version_ok=order[declared] >= order[required]
    )
