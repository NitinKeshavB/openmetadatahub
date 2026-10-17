from conftest import CONTRACT_V1

from openmetadatahub.access import Finding
from openmetadatahub.ai import (
    AIAsset,
    AIPolicy,
    AssetKind,
    CheckStatus,
    Comparator,
    EvaluationThreshold,
    Evidence,
    RiskClass,
    Stage,
    assess,
    control_coverage,
    discover_odh,
)
from openmetadatahub.common import Severity
from openmetadatahub.contracts import load_contract

MANIFESTS = """
apiVersion: v1
kind: List
items:
  - apiVersion: serving.kserve.io/v1beta1
    kind: InferenceService
    metadata:
      name: churn-model
      namespace: ds-prod
      annotations:
        openmetadatahub.io/owner: ml-team
        openmetadatahub.io/approved-by: cdo
        openmetadatahub.io/risk-class: high
        openmetadatahub.io/stage: production
        openmetadatahub.io/intended-use: churn scoring for retention offers
        openmetadatahub.io/limitations: not validated for business accounts
        openmetadatahub.io/human-oversight: retention agent reviews every offer
        openmetadatahub.io/data-sources: main.sales.orders, main.sales.customers
    status:
      url: https://churn.example/v1/models/churn
  - apiVersion: serving.kserve.io/v1beta1
    kind: InferenceService
    metadata: {name: shadow-llm, namespace: ds-prod}
  - apiVersion: trustyai.opendatahub.io/v1alpha1
    kind: TrustyAIService
    metadata: {name: trustyai-service, namespace: ds-prod}
  - apiVersion: trustyai.opendatahub.io/v1alpha1
    kind: LMEvalJob
    metadata: {name: eval1, namespace: ds-prod}
    spec:
      model: churn-model
      taskList: {taskNames: [arc_easy, truthfulqa]}
"""


def test_discovery_reads_annotations_and_trustyai_context():
    assets = {a.name: a for a in discover_odh(MANIFESTS)}
    churn = assets["churn-model"]
    assert churn.stage == Stage.PRODUCTION and churn.risk_class == RiskClass.HIGH
    assert churn.data_sources == ["main.sales.orders", "main.sales.customers"]
    assert churn.monitoring_enabled and not churn.guardrails_enabled
    assert churn.labels["evaluation_tasks"] == "arc_easy,truthfulqa"
    assert churn.endpoint == "https://churn.example/v1/models/churn"


def test_ungoverned_model_is_surfaced_as_unauthorized():
    shadow = {a.name: a for a in discover_odh(MANIFESTS)}["shadow-llm"]
    assert shadow.stage == Stage.UNAUTHORIZED and shadow.owner is None
    result = assess(shadow, AIPolicy())
    assert result.blocked
    assert any(c.code == "unregistered-asset" and c.severity == Severity.CRITICAL for c in result.checks)


def _healthy_evidence():
    contract = load_contract(CONTRACT_V1)
    return Evidence(
        quality_scores={"main.sales.orders": 97.0, "main.sales.customers": 95.0},
        contracts={contract.id: contract},
        table_contracts={"main.sales.orders": contract.id, "main.sales.customers": contract.id},
    )


def test_well_governed_asset_is_not_blocked():
    churn = {a.name: a for a in discover_odh(MANIFESTS)}["churn-model"].model_copy(
        update={"evaluations": {"bias_score": 0.05}}
    )
    policy = AIPolicy(thresholds=[EvaluationThreshold(metric="bias_score", comparator=Comparator.LTE, value=0.1)])
    result = assess(churn, policy, _healthy_evidence())
    assert not result.blocked, [c for c in result.checks if c.status == CheckStatus.FAIL]
    assert result.summary.get("fail", 0) == 0


def test_data_quality_gate_blocks_the_model_not_just_the_table():
    churn = {a.name: a for a in discover_odh(MANIFESTS)}["churn-model"]
    ev = _healthy_evidence()
    ev.quality_scores["main.sales.orders"] = 72.0
    del ev.quality_scores["main.sales.customers"]
    result = assess(churn, AIPolicy(), ev)
    by = {c.code: c for c in result.checks}
    assert by["quality:main.sales.orders"].status == CheckStatus.FAIL
    assert by["quality:main.sales.customers"].message == "no data quality result for this source"
    assert result.blocked


def test_access_findings_on_source_tables_block_the_asset():
    churn = {a.name: a for a in discover_odh(MANIFESTS)}["churn-model"]
    ev = _healthy_evidence()
    ev.access_findings["main.sales.orders"] = [
        Finding(table="main.sales.orders", code="forbidden-grantee", severity=Severity.CRITICAL, message="x"),
        Finding(table="main.sales.orders", code="direct-user-grant", severity=Severity.WARNING, message="y"),
    ]
    by = {c.code: c for c in assess(churn, AIPolicy(), ev).checks}
    assert by["access:main.sales.orders"].status == CheckStatus.FAIL
    assert "forbidden-grantee" in by["access:main.sales.orders"].message
    assert "direct-user-grant" not in by["access:main.sales.orders"].message  # below the gate
    assert by["access:main.sales.customers"].status == CheckStatus.PASS


def test_missing_evaluation_result_fails_rather_than_passes():
    a = AIAsset(id="a", name="a", kind=AssetKind.MODEL, owner="o")
    policy = AIPolicy(
        thresholds=[EvaluationThreshold(metric="hallucination_rate", comparator=Comparator.LTE, value=0.05)]
    )
    check = next(c for c in assess(a, policy).checks if c.code == "eval:hallucination_rate")
    assert check.status == CheckStatus.FAIL and "no evaluation result" in check.message


def test_threshold_directions():
    a = AIAsset(
        id="a", name="a", kind=AssetKind.MODEL, owner="o", evaluations={"accuracy": 0.8, "pii_leakage_rate": 0.02}
    )
    policy = AIPolicy(
        thresholds=[
            EvaluationThreshold(metric="accuracy", comparator=Comparator.GTE, value=0.9),
            EvaluationThreshold(
                metric="pii_leakage_rate", comparator=Comparator.LTE, value=0.01, severity=Severity.CRITICAL
            ),
        ]
    )
    by = {c.code: c for c in assess(a, policy).checks}
    assert by["eval:accuracy"].status == CheckStatus.FAIL
    assert by["eval:pii_leakage_rate"].severity == Severity.CRITICAL


def test_unacceptable_risk_is_always_blocked_even_if_policy_scopes_it_out():
    a = AIAsset(id="a", name="a", kind=AssetKind.APPLICATION, owner="o", risk_class=RiskClass.UNACCEPTABLE)
    assert assess(a, AIPolicy(applies_to_risk=[RiskClass.HIGH])).blocked


def test_control_coverage_never_treats_manual_or_missing_evidence_as_pass():
    a = AIAsset(
        id="a",
        name="a",
        kind=AssetKind.MODEL,
        owner="o",
        risk_class=RiskClass.HIGH,
        stage=Stage.STAGING,
        intended_use="u",
        limitations="l",
        human_oversight="h",
        monitoring_enabled=True,
    )
    cov = control_coverage(assess(a, AIPolicy()))
    assert cov["EUAIA-10"]["status"] == "manual"  # no data sources declared
    assert cov["EUAIA-14"]["status"] == "pass"
    assert cov["EUAIA-12"]["status"] == "pass"
    assert cov["EUAIA-5"]["status"] == "no_evidence"
