"""Evaluate an access snapshot against classification rules and (optionally) a data contract's roles.

All remediation is returned as dry-run SQL text. This library has no write path to the warehouse.
"""

from __future__ import annotations

from ..common import Severity, quote_fqn, quote_ident
from ..contracts.odcs import DataContract
from .models import AccessPolicy, AccessSnapshot, Finding

_READ = {"SELECT", "ALL PRIVILEGES"}
_WRITE = {"MODIFY", "ALL PRIVILEGES"}


def effective_privileges(snap: AccessSnapshot) -> dict[str, set[str]]:
    """Union of privileges per principal across catalog, schema and table (inherited grants apply downward)."""
    out: dict[str, set[str]] = {}
    for g in snap.grants:
        out.setdefault(g.principal, set()).add(g.privilege)
    return out


def _revoke(table: str, priv: str, principal: str) -> str:
    return f"REVOKE {priv} ON TABLE {quote_fqn(table)} FROM {quote_ident(principal)};"


def evaluate(snap: AccessSnapshot, policy: AccessPolicy, contract: DataContract | None = None,
             contract_object: str | None = None) -> list[Finding]:
    f: list[Finding] = []
    admins = set(policy.admin_principals)
    eff = effective_privileges(snap)
    t = snap.table
    masked = {m.column.lower() for m in snap.column_masks}

    for c in snap.caveats:
        f.append(Finding(table=t, code="snapshot-caveat", severity=Severity.WARNING, message=c))

    # Hygiene
    for g in snap.grants:
        if g.principal in admins:
            continue
        if policy.flag_direct_user_grants and "@" in g.principal:
            rem = _revoke(t, g.privilege, g.principal) if g.level == "table" and not g.inherited_from else None
            f.append(Finding(table=t, code="direct-user-grant", severity=Severity.WARNING, principal=g.principal,
                             message=f"{g.privilege} granted to an individual user; grant to a group instead", remedy=rem))

    # Classification-driven
    for rule in policy.classification_rules:
        wanted = {v.lower() for v in rule.values}
        for tag in snap.tags:
            if tag.name != rule.tag or (tag.value or "").lower() not in wanted or not tag.column:
                continue
            col = tag.column
            readers = [p for p, privs in eff.items() if privs & _READ and p not in admins]
            is_masked = col.lower() in masked
            if rule.require_mask and not is_masked:
                exposed = [p for p in readers if p not in rule.allowed_principals]
                hint = (f"ALTER TABLE {quote_fqn(t)} ALTER COLUMN {quote_ident(col)} SET MASK {quote_fqn(rule.mask_function)};"
                        if rule.mask_function else None)
                if exposed:
                    f.append(Finding(table=t, column=col, code="unmasked-sensitive-column", severity=Severity.ERROR,
                                     message=f"{rule.tag}={tag.value} column has no column mask and is readable by: {', '.join(sorted(exposed))}",
                                     remedy=hint))
                else:
                    f.append(Finding(table=t, column=col, code="unmasked-sensitive-column", severity=Severity.INFO,
                                     message=f"{rule.tag}={tag.value} column has no column mask; only allowed principals can read it today",
                                     remedy=hint))
            if not is_masked:
                for p in rule.deny_principals:
                    if eff.get(p, set()) & _READ:
                        f.append(Finding(table=t, column=col, principal=p, code="forbidden-grantee", severity=Severity.CRITICAL,
                                         message=f"{p} can read unmasked {rule.tag}={tag.value} column",
                                         remedy=_revoke(t, "SELECT", p)))

    # Contract roles
    if contract is not None:
        f += _contract_findings(snap, policy, contract, contract_object, eff, admins)
    return f


def _contract_findings(snap, policy, contract, obj_name, eff, admins) -> list[Finding]:
    out: list[Finding] = []
    t = snap.table
    cat, sch, _ = t.split(".")
    expected: dict[str, set[str]] = {}
    for role in contract.roles:
        principal = policy.role_principals.get(role.role, role.role)
        needs = {"SELECT"} if (role.access or "read").lower() == "read" else {"SELECT", "MODIFY"}
        expected.setdefault(principal, set()).update(needs)

    for principal, needs in expected.items():
        have = eff.get(principal, set())
        for priv in sorted(needs):
            ok = priv in have or "ALL PRIVILEGES" in have
            if not ok:
                out.append(Finding(table=t, principal=principal, code="missing-grant", severity=Severity.ERROR,
                                   message=f"contract role requires {priv} but {principal} does not hold it",
                                   remedy=f"GRANT {priv} ON TABLE {quote_fqn(t)} TO {quote_ident(principal)};"))
        usage = {p.privilege for p in snap.grants if p.principal == principal and p.level in ("catalog", "schema")}
        if "ALL PRIVILEGES" not in usage:
            if "USE CATALOG" not in usage:
                out.append(Finding(table=t, principal=principal, code="missing-usage", severity=Severity.WARNING,
                                   message=f"{principal} lacks USE CATALOG on {cat}; table grants alone are not enough",
                                   remedy=f"GRANT USE CATALOG ON CATALOG {quote_ident(cat)} TO {quote_ident(principal)};"))
            if "USE SCHEMA" not in usage:
                out.append(Finding(table=t, principal=principal, code="missing-usage", severity=Severity.WARNING,
                                   message=f"{principal} lacks USE SCHEMA on {cat}.{sch}",
                                   remedy=f"GRANT USE SCHEMA ON SCHEMA {quote_ident(cat)}.{quote_ident(sch)} TO {quote_ident(principal)};"))

    for g in snap.grants:
        if g.principal in admins or g.principal in expected or g.privilege not in (_READ | _WRITE | {"MODIFY"}):
            continue
        origin = f"inherited from {g.inherited_from}" if g.inherited_from else f"direct {g.level}-level grant"
        rem = _revoke(t, g.privilege, g.principal) if (g.level == "table" and not g.inherited_from) else None
        out.append(Finding(table=t, principal=g.principal, code="unexpected-grant", severity=Severity.WARNING,
                           message=f"{g.principal} holds {g.privilege} ({origin}) but no contract role maps to it", remedy=rem))
    return out
