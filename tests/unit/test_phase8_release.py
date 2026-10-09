"""Stateful release failures verify stop ordering, exact revision and restoration."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from scripts.release import Release, ReleaseError, registration

REPO = "123456789012.dkr.ecr.us-east-1.amazonaws.com/finbot-ingestion"
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


def definition(digest=OLD):
    return {"taskDefinitionArn": "old", "family": "finbot-ingestion", "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"], "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "taskRoleArn": "task-role", "executionRoleArn": "execution-role", "cpu": "512", "memory": "1024",
        "containerDefinitions": [{"name": "ingestion", "essential": True, "image": REPO + "@" + digest,
            "user": "10001:10001", "readonlyRootFilesystem": True, "stopTimeout": 120,
            "healthCheck": {"startPeriod": 300, "command": ["CMD", "python", "-m", "finbot_ingestion.main", "--health-check"]},
            "environment": [{"name": "RUNTIME_SEC_STARTUP_QUIET_SECONDS", "value": "150"},
                {"name": "SEC_MAX_REQUESTS_PER_SECOND", "value": "5"}]}]}


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class ECS:
    def __init__(self, *, count=1, failure=None):
        self.count, self.revision = count, "old"
        self.definitions = {"old": definition(), "baseline": definition()}
        self.definitions["baseline"]["taskDefinitionArn"] = "baseline"
        self.records = {"old-task": self.task("old-task", "old")} if count else {}
        self.trace, self.failure, self.failed = [], failure, False
        self.stopping_ticks = 0
        self.listed_empty_pages = 0

    def task(self, arn, revision):
        digest = self.definitions[revision]["containerDefinitions"][0]["image"].split("@")[1]
        return {"taskArn": arn, "taskDefinitionArn": revision, "lastStatus": "RUNNING", "healthStatus": "HEALTHY",
            "containers": [{"name": "ingestion", "imageDigest": digest, "healthStatus": "HEALTHY"}]}

    def describe_task_definition(self, taskDefinition):
        return {"taskDefinition": deepcopy(self.definitions[taskDefinition])}

    def describe_services(self, **kwargs):
        self.stopping_ticks += 1
        if self.stopping_ticks > 2 and self.failure != "never_stops":
            for task in self.records.values():
                if task["lastStatus"] == "STOPPING":
                    task["lastStatus"] = "STOPPED"
                    self.trace.append(("stopped", task["taskArn"]))
        rollout = "COMPLETED"
        if self.revision == "new" and self.count and self.failure == "circuit":
            rollout = "FAILED"
        return {"services": [{"status": "ACTIVE", "desiredCount": self.count, "taskDefinition": self.revision,
            "runningCount": sum(t["lastStatus"] == "RUNNING" for t in self.records.values()), "pendingCount": 0,
            "deploymentConfiguration": {"minimumHealthyPercent": 0, "maximumPercent": 100},
            "deployments": [{"status": "PRIMARY", "taskDefinition": self.revision, "rolloutState": rollout}]}]}

    def get_paginator(self, name):
        assert name == "list_tasks"
        def paginate(**kwargs):
            self.listed_empty_pages += 1
            yield {"taskArns": [], "nextToken": "more"}
            if kwargs["desiredStatus"] == "RUNNING":
                yield {"taskArns": [k for k, v in self.records.items() if v["lastStatus"] == "RUNNING"]}
            else:
                yield {"taskArns": [k for k, v in self.records.items() if v["lastStatus"] != "RUNNING"]}
        return SimpleNamespace(paginate=paginate)

    def describe_tasks(self, tasks, **kwargs):
        return {"tasks": [deepcopy(self.records[t]) for t in tasks]}

    def register_task_definition(self, **kwargs):
        self.trace.append(("register", "new"))
        self.definitions["new"] = {**deepcopy(kwargs), "taskDefinitionArn": "new"}
        return {"taskDefinition": deepcopy(self.definitions["new"])}

    def update_service(self, desiredCount, taskDefinition=None, **kwargs):
        self.trace.append(("update", desiredCount, taskDefinition))
        self.count = desiredCount
        if desiredCount == 0:
            self.stopping_ticks = 0
            for t in self.records.values():
                if t["lastStatus"] != "STOPPED":
                    t["lastStatus"] = "STOPPING"
        if taskDefinition:
            self.revision = taskDefinition
        if desiredCount:
            assert all(t["lastStatus"] == "STOPPED" for t in self.records.values()), "started before STOPPED"
            self.records[self.revision + "-task"] = self.task(self.revision + "-task", self.revision)
            if self.revision == "new" and self.failure == "wrong_digest":
                self.records["new-task"]["containers"][0]["imageDigest"] = OLD
            if self.revision == "new" and self.failure in ("lost_start_ack", "cancel") and not self.failed:
                self.failed = True
                if self.failure == "cancel":
                    raise KeyboardInterrupt()
                raise RuntimeError("lost acknowledgment")
        return {"service": {}}


class ECR:
    def describe_images(self, **kwargs):
        return {"imageDetails": [{"imageDigest": NEW,
            "imageManifestMediaType": "application/vnd.docker.distribution.manifest.v2+json"}]}


def runner(tmp_path, **options):
    ecs, clock = ECS(**options), Clock()
    release = Release(ecs=ecs, ecr=ECR(), cluster="cluster", service="service", repository_uri=REPO,
        baseline="baseline", manifest_path=tmp_path / "release.json", timeout=8, poll_seconds=1,
        clock=clock, sleep=clock.sleep)
    return release, ecs


def test_stop_before_start_and_verified_digest(tmp_path):
    release, ecs = runner(tmp_path)
    result = release.deploy(NEW, activation_approved=True)
    assert result["outcome"] == "running" and result["checkpoint"] == "verified"
    assert ecs.trace.index(("stopped", "old-task")) < ecs.trace.index(("update", 1, "new"))
    assert ecs.trace[0] == ("register", "new")
    assert ecs.listed_empty_pages
    assert json.loads((tmp_path / "release.json").read_text())["previous_revision"] == "old"


def test_stopped_service_stays_stopped(tmp_path):
    release, ecs = runner(tmp_path, count=0)
    assert release.deploy(NEW)["outcome"] == "staged"
    assert not any(t[1] == 1 for t in ecs.trace if t[0] == "update")


def test_initial_activation_requires_explicit_gate(tmp_path):
    release, ecs = runner(tmp_path, count=0)
    with pytest.raises(ReleaseError):
        release.deploy(NEW, activate=True)
    assert not ecs.trace
    assert release.deploy(NEW, activate=True, activation_approved=True)["outcome"] == "running"


@pytest.mark.parametrize("failure", ["wrong_digest", "circuit", "lost_start_ack", "cancel"])
def test_failure_restores_original_but_never_reports_success(tmp_path, failure):
    release, ecs = runner(tmp_path, failure=failure)
    with pytest.raises((ReleaseError, RuntimeError, KeyboardInterrupt)):
        release.deploy(NEW, activation_approved=True)
    assert release.manifest["outcome"] == "failed_restored"
    assert ecs.revision == "old" and ecs.count == 1
    assert ecs.trace.index(("stopped", "new-task")) < len(ecs.trace) - 1


def test_unproved_stopping_fails_closed(tmp_path):
    release, ecs = runner(tmp_path, failure="never_stops")
    with pytest.raises(ReleaseError):
        release.deploy(NEW, activation_approved=True)
    assert ecs.count == 0 and ecs.revision == "old"
    assert release.manifest["outcome"] == "failed_restore_unverified"
    assert not any(t[1] == 1 for t in ecs.trace if t[0] == "update")


def test_config_drift_and_multiple_tasks_fail_before_mutation(tmp_path):
    release, ecs = runner(tmp_path)
    ecs.definitions["old"]["cpu"] = "1024"
    with pytest.raises(ReleaseError, match="differs"):
        release.deploy(NEW, activation_approved=True)
    assert not ecs.trace
    ecs.definitions["old"]["cpu"] = "512"
    ecs.records["unexpected"] = ecs.task("unexpected", "old")
    with pytest.raises(ReleaseError, match="settled"):
        release.deploy(NEW, activation_approved=True)
    assert not ecs.trace


def test_missing_task_description_is_not_quiescence(tmp_path):
    release, ecs = runner(tmp_path)
    ecs.describe_tasks = lambda **kwargs: {"tasks": [], "failures": [{"reason": "MISSING"}]}
    with pytest.raises(ReleaseError, match="prove"):
        release.deploy(NEW, activation_approved=True)
    assert not ecs.trace


def test_register_fields_match_real_sdk_model_without_metadata():
    from contextlib import closing
    import boto3
    from botocore.config import Config
    from botocore.validate import validate_parameters
    with closing(boto3.client("ecs", region_name="us-east-1", config=Config())) as client:
        shape = client.meta.service_model.operation_model("RegisterTaskDefinition").input_shape
        validate_parameters(registration(definition()), shape)
    assert "taskDefinitionArn" not in registration(definition())


def test_sigterm_maps_to_cancellation_and_restores_handler():
    import os
    import signal
    from scripts.release import release_signals
    previous = signal.getsignal(signal.SIGTERM)
    with release_signals():
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_failed_stopped_service_can_stage_manual_recovery(tmp_path):
    release, ecs = runner(tmp_path, count=0)
    describe = ecs.describe_services
    def failed(**kwargs):
        result = describe(**kwargs)
        if ecs.revision == "old":
            result["services"][0]["deployments"][0]["rolloutState"] = "FAILED"
        return result
    ecs.describe_services = failed
    assert release.deploy(NEW)["outcome"] == "staged"
