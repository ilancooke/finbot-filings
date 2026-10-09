"""Guard the sensitive trigger/credential boundaries of application delivery."""
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_ci_and_delivery_workflow_contracts():
    workflows = {name: yaml.load((ROOT / ".github/workflows" / (name + ".yml")).read_text(), Loader=yaml.BaseLoader)
        for name in ("ci", "deploy")}
    ci, deploy = workflows["ci"], workflows["deploy"]
    assert "pull_request" in ci["on"] and "push" in ci["on"]
    assert "pull_request_target" not in ci["on"] and "pull_request" not in deploy["on"]
    assert ci["permissions"] == {"contents": "read"}
    job = deploy["jobs"]["release"]
    assert job["environment"] == "production" and job["permissions"]["id-token"] == "write"
    assert "FINBOT_DELIVERY_ENABLED" in job["if"] and "head_repository.full_name == github.repository" in job["if"]
    assert "conclusion == 'success'" in job["if"]
    assert deploy["concurrency"]["cancel-in-progress"] == "false"
    assert deploy["on"]["workflow_dispatch"]["inputs"]["activate"]["default"] == "false"
    for workflow in workflows.values():
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", step["uses"])
                assert "cdk deploy" not in step.get("run", "")
    commands = "\n".join(s.get("run", "") for s in ci["jobs"]["validate"]["steps"])
    assert "--no-lookups" in commands and "FINBOT_CONTAINER_TESTS=1" in commands
    assert "scripts/check_heartbeat_volume.py" in commands
