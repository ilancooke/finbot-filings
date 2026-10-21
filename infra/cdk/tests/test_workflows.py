"""Guard the sensitive trigger/credential boundaries of application delivery."""
from pathlib import Path
import json
import re

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_ci_and_delivery_workflow_contracts():
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert not (ROOT / ".github/workflows/deploy.yml").exists()
    assert not (ROOT / "scripts/deliver.py").exists()
    assert "pull_request" in ci["on"] and "push" in ci["on"]
    assert ci["on"]["push"]["branches"] == ["main"]
    assert "pull_request_target" not in ci["on"] and "workflow_run" not in ci["on"]
    assert ci["permissions"] == {"contents": "read"}
    validate = ci["jobs"]["validate"]
    assert "environment" not in validate and "permissions" not in validate
    job = ci["jobs"]["release"]
    assert job["needs"] == "validate"
    assert job["environment"] == "production" and job["permissions"]["id-token"] == "write"
    for gate in ("FINBOT_DELIVERY_ENABLED == 'true'", "github.ref == 'refs/heads/main'",
                 "github.event_name == 'push'", "github.event_name == 'workflow_dispatch'"):
        assert gate in job["if"]
    assert "always()" not in job["if"]  # A failed validation must prevent deployment.
    assert job["concurrency"]["cancel-in-progress"] == "false"
    assert ci["on"]["workflow_dispatch"]["inputs"]["activate"]["default"] == "false"
    for workflow_job in ci["jobs"].values():
        for step in workflow_job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", step["uses"])
            assert "cdk deploy" not in step.get("run", "")
    commands = "\n".join(s.get("run", "") for s in validate["steps"])
    assert "--no-lookups" in commands and "FINBOT_CONTAINER_TESTS=1" in commands
    assert "scripts/check_heartbeat_volume.py" in commands
    assert commands.index("test_container_lifecycle.py") < commands.index("docker save")
    assert commands.count("docker build") == 1
    release_commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert "docker build" not in release_commands
    assert "docker load" in release_commands and "scripts/release.py --github" in release_commands
    assert job["env"]["TESTED_IMAGE_ID"] == "${{ needs.validate.outputs.image_id }}"
    uploaded = next(s for s in validate["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    downloaded = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
    assert uploaded["with"]["name"] == "${{ steps.exported_image.outputs.artifact_name }}"
    assert validate["outputs"]["image_artifact"] == uploaded["with"]["name"]
    # Retrying only a failed release must use the original validation attempt's
    # artifact, not look for an image from the new release attempt.
    assert downloaded["with"]["name"] == "${{ needs.validate.outputs.image_artifact }}"
    assert "GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT" in commands


def test_production_has_no_routine_review_and_retains_main_branch_restriction():
    environment = json.loads((ROOT / "infra/github/production-environment.json").read_text())
    assert environment["reviewers"] == [] and environment["wait_timer"] == 0
    assert environment["deployment_branch_policy"] == {
        "protected_branches": False, "custom_branch_policies": True}
    policy = json.loads((ROOT / "infra/github/production-branch-policy.json").read_text())
    assert policy == {"name": "main", "type": "branch"}
