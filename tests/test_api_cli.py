import json

from conftest import CONTRACT_V1
from fastapi.testclient import TestClient
from test_ai import MANIFESTS

from openmetadatahub.api import create_app
from openmetadatahub.cli import main


def test_api_end_to_end(fake):
    fake.when(
        "information_schema.columns",
        [
            {"column_name": "order_id", "data_type": "BIGINT", "is_nullable": "NO"},
            {"column_name": "email", "data_type": "STRING", "is_nullable": "YES"},
            {"column_name": "status", "data_type": "STRING", "is_nullable": "NO"},
            {"column_name": "updated_at", "data_type": "TIMESTAMP", "is_nullable": "YES"},
        ],
    )
    fake.when("IS NULL", [{"total_rows": 10, "failed_rows": 0}])
    fake.when("GROUP BY", [{"total_rows": 10, "failed_rows": 0}])
    fake.when("RLIKE", [{"total_rows": 10, "failed_rows": 0}])
    fake.when("NOT IN", [{"total_rows": 10, "failed_rows": 0}])
    fake.when("UNIX_TIMESTAMP", [{"observed": 3.0}])
    fake.when("FROM (SELECT COUNT(*)", [{"observed": 0}])
    fake.when("COUNT(*) AS observed", [{"observed": 10}])
    client = TestClient(create_app(fake))

    assert client.get("/health").json()["status"] == "ok"
    assert len(client.get("/dq/catalog").json()) == 18

    r = client.post("/contracts/validate", json={"contract_yaml": CONTRACT_V1})
    assert r.status_code == 200
    body = r.json()
    assert body["schema_issues"] == []
    assert body["quality"]["score"] == 100.0 and body["quality"]["aborted"] == 0

    d = client.post("/contracts/diff", json={"old_yaml": CONTRACT_V1, "new_yaml": CONTRACT_V1})
    assert d.json()["version_ok"] is True

    assert client.post("/contracts/tests", json={"contract_yaml": "- not a mapping"}).status_code == 422

    found = client.post("/ai/discover", json={"manifests_yaml": MANIFESTS}).json()
    assert {a["name"] for a in found} == {"churn-model", "shadow-llm"}
    shadow = next(a for a in found if a["name"] == "shadow-llm")
    out = client.post("/ai/assess", json={"asset": shadow}).json()
    assert out["assessment"]["blocked"] is True
    assert out["control_coverage"]["EUAIA-5"]["status"] == "no_evidence"


def test_api_without_a_warehouse_fails_loudly_not_silently():
    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.post(
        "/dq/run",
        json={
            "table": "a.b.c",
            "cases": [{"name": "n", "table": "a.b.c", "metric": "row_count", "params": {"min": 1}}],
        },
    )
    assert r.status_code == 200 and r.json()["aborted"] == 1  # reported as aborted, not as a pass


def test_cli_contract_diff_exit_code(tmp_path, capsys):
    old, new = tmp_path / "old.yaml", tmp_path / "new.yaml"
    old.write_text(CONTRACT_V1)
    new.write_text(CONTRACT_V1.replace("      - name: updated_at\n        logicalType: timestamp\n", ""))
    assert main(["contract-diff", str(old), str(new)]) == 1  # breaking change without a major bump
    assert json.loads(capsys.readouterr().out)["required_bump"] == "major"
    assert main(["contract-diff", str(old), str(old)]) == 0


def test_cli_compile_and_discover(tmp_path, capsys):
    c = tmp_path / "c.yaml"
    c.write_text(CONTRACT_V1)
    assert main(["contract-tests", str(c)]) == 0
    assert any(t["metric"] == "freshness_hours" for t in json.loads(capsys.readouterr().out))
    m = tmp_path / "m.yaml"
    m.write_text(MANIFESTS)
    assert main(["ai-discover", str(m)]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
