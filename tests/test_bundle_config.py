"""The bundle follows the cookiecutter-dbxops delivery process: five targets, tags, budget policy, notifications."""

import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["dev", "test", "uat", "preprod", "prod"]
TAGS = {"project_id", "workload", "bundle", "target", "git_repo", "git_ref", "git_sha", "cost_center", "region"}


def load_bundle() -> dict:
    """Return databricks.yml with every included resource file merged under `resources`."""
    root = yaml.safe_load((ROOT / "databricks.yml").read_text())
    merged: dict = {}
    for pattern in root.pop("include", []):
        for path in sorted(glob.glob(str(ROOT / pattern))):
            for kind, items in yaml.safe_load(Path(path).read_text())["resources"].items():
                merged.setdefault(kind, {}).update(items)
    root["resources"] = merged
    return root


BUNDLE = load_bundle()
JOBS = BUNDLE["resources"]["jobs"]


def test_five_targets_with_service_principals_outside_dev():
    assert list(BUNDLE["targets"]) == TARGETS
    for name, target in BUNDLE["targets"].items():
        assert target["mode"] == ("development" if name == "dev" else "production"), name
        assert ("run_as" in target) == (name != "dev"), name
        assert target["variables"]["schedule_status" if name == "prod" else "results_catalog"]


@pytest.mark.parametrize("key", sorted(JOBS))
def test_every_job_is_tagged_attributed_and_notifies(key):
    job = JOBS[key]
    assert set(job["tags"]) == TAGS and job["tags"]["git_sha"] == "${bundle.git.commit}"
    assert job["budget_policy_id"] == "${var.budget_policy_id}"
    assert job["email_notifications"]["on_failure"] == ["${var.notify_email}"]
    assert job["webhook_notifications"]["on_failure"] == [{"id": "${var.notification_destination_id}"}]
    assert job["name"].startswith("[${var.project_id}] ${var.workload} ")  # lets cost attribution read the project


def test_unknown_values_are_tracked_placeholders_that_block_deploys():
    text = (ROOT / "databricks.yml").read_text()
    assert "REPLACE-ME" not in text and "example.com" not in text
    for name in TARGETS:
        variables = BUNDLE["targets"][name]["variables"]
        for key in ("notify_email", "notification_destination_id", "budget_policy_id", "cost_center", "region"):
            assert variables[key].startswith("PLACEHOLDER-"), (name, key)


def test_the_bundle_matches_the_databricks_cli_schema():
    if shutil.which("databricks") is None:
        pytest.skip("databricks CLI not installed")
    jsonschema = pytest.importorskip("jsonschema")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}
    schema = json.loads(subprocess.run(["databricks", "bundle", "schema"], capture_output=True, text=True,
                                       check=True, env=env).stdout)
    text = re.sub(r"\[\\\\p\{L\}\\\\p\{N\}\]", "[A-Za-z0-9]", json.dumps(schema)).replace("\\\\p{L}", "[A-Za-z]")
    errors = list(jsonschema.Draft7Validator(json.loads(text)).iter_errors(BUNDLE))
    assert not errors, [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in errors]
