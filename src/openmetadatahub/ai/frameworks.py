"""Control library and coverage roll-up.

References are deliberately coarse: EU AI Act articles 5 and 9-15 (the prohibited-practice and
high-risk requirement articles) and the four NIST AI RMF functions. A passing check is *evidence
toward* a control. It is not a legal or audit determination of compliance.
"""

from __future__ import annotations

from pydantic import BaseModel

from .models import Assessment, CheckStatus


class Control(BaseModel):
    id: str
    framework: str
    title: str


CONTROLS: dict[str, Control] = {c.id: c for c in [
    Control(id="EUAIA-5", framework="EU AI Act", title="Art. 5 Prohibited AI practices"),
    Control(id="EUAIA-9", framework="EU AI Act", title="Art. 9 Risk management system"),
    Control(id="EUAIA-10", framework="EU AI Act", title="Art. 10 Data and data governance"),
    Control(id="EUAIA-11", framework="EU AI Act", title="Art. 11 Technical documentation"),
    Control(id="EUAIA-12", framework="EU AI Act", title="Art. 12 Record-keeping"),
    Control(id="EUAIA-13", framework="EU AI Act", title="Art. 13 Transparency and information to deployers"),
    Control(id="EUAIA-14", framework="EU AI Act", title="Art. 14 Human oversight"),
    Control(id="EUAIA-15", framework="EU AI Act", title="Art. 15 Accuracy, robustness and cybersecurity"),
    Control(id="NIST-GOVERN", framework="NIST AI RMF", title="GOVERN: policies, accountability, culture"),
    Control(id="NIST-MAP", framework="NIST AI RMF", title="MAP: context and risk framing"),
    Control(id="NIST-MEASURE", framework="NIST AI RMF", title="MEASURE: analysis and monitoring of risk"),
    Control(id="NIST-MANAGE", framework="NIST AI RMF", title="MANAGE: prioritise and act on risk"),
]}


def control_coverage(assessment: Assessment) -> dict[str, dict]:
    """Per control: fail > manual > pass; `no_evidence` when no automated check touches it."""
    out: dict[str, dict] = {}
    for cid, ctl in CONTROLS.items():
        related = [c for c in assessment.checks if cid in c.control_refs and c.status != CheckStatus.NOT_APPLICABLE]
        if not related:
            status = "no_evidence"
        elif any(c.status == CheckStatus.FAIL for c in related):
            status = "fail"
        elif any(c.status == CheckStatus.MANUAL for c in related):
            status = "manual"
        else:
            status = "pass"
        out[cid] = {"framework": ctl.framework, "title": ctl.title, "status": status,
                    "checks": [c.code for c in related]}
    return out
