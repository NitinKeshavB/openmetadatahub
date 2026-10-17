"""HTTP API. The warehouse executor is injected, so the app is testable without Databricks."""

from __future__ import annotations

from fastapi import (
    FastAPI,
    HTTPException,
)
from pydantic import (
    BaseModel,
    Field,
)

from . import __version__
from .access import (
    AccessPolicy,
    evaluate,
    snapshot_table,
)
from .ai import (
    AIAsset,
    AIPolicy,
    Evidence,
    assess,
    control_coverage,
    discover_odh,
)
from .common import SqlExecutor
from .contracts import (
    contract_to_tests,
    diff_contracts,
    load_contract,
    validate_schema,
)
from .dq import (
    MetricError,
    TestCase,
    describe_catalog,
    run_cases,
    summarise,
)


class _NoWarehouse:
    def query(self, sql: str):
        raise RuntimeError("no warehouse configured: create_app(executor=...)")


class ContractBody(BaseModel):
    contract_yaml: str
    namespace: str | None = Field(default=None, description="catalog.schema for unqualified physicalNames")


class DiffBody(BaseModel):
    old_yaml: str
    new_yaml: str


class RunBody(BaseModel):
    table: str
    cases: list[TestCase]


class AccessBody(BaseModel):
    table: str
    policy_yaml: str = ""
    contract_yaml: str | None = None


class DiscoverBody(BaseModel):
    manifests_yaml: str


class AssessBody(BaseModel):
    asset: AIAsset
    policy: AIPolicy = Field(default_factory=AIPolicy)
    quality_scores: dict[str, float] = Field(default_factory=dict)
    table_contracts: dict[str, str] = Field(default_factory=dict)


def create_app(executor: SqlExecutor | None = None) -> FastAPI:
    ex: SqlExecutor = executor or _NoWarehouse()
    app = FastAPI(title="openmetadatahub", version=__version__)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/dq/catalog")
    def catalog():
        return describe_catalog()

    @app.post("/dq/run")
    def dq_run(body: RunBody):
        return summarise(body.table, run_cases(body.cases, ex))

    @app.post("/contracts/tests")
    def contract_tests(body: ContractBody):
        try:
            return contract_to_tests(load_contract(body.contract_yaml), body.namespace)
        except (ValueError, MetricError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/contracts/validate")
    def contract_validate(body: ContractBody):
        try:
            c = load_contract(body.contract_yaml)
            issues = validate_schema(c, ex, body.namespace)
            report = summarise(c.id, run_cases(contract_to_tests(c, body.namespace), ex))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"schema_issues": issues, "quality": report}

    @app.post("/contracts/diff")
    def contract_diff(body: DiffBody):
        try:
            return diff_contracts(load_contract(body.old_yaml), load_contract(body.new_yaml))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/access/evaluate")
    def access_evaluate(body: AccessBody):
        try:
            policy = AccessPolicy.from_yaml(body.policy_yaml)
            contract = load_contract(body.contract_yaml) if body.contract_yaml else None
            snap = snapshot_table(ex, body.table)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"snapshot": snap, "findings": evaluate(snap, policy, contract)}

    @app.post("/ai/discover")
    def ai_discover(body: DiscoverBody):
        return discover_odh(body.manifests_yaml)

    @app.post("/ai/assess")
    def ai_assess(body: AssessBody):
        ev = Evidence(quality_scores=body.quality_scores, table_contracts=body.table_contracts)
        result = assess(body.asset, body.policy, ev)
        return {"assessment": result, "control_coverage": control_coverage(result)}

    return app
