"""Explicit stop/wait/start application release. Never provisions infrastructure.

Imports are inert; CLI execution uses AWS. Tests inject clients and clocks.
"""
import argparse
from copy import deepcopy
from contextlib import closing, contextmanager
import json
import math
from pathlib import Path
import re
import signal
import sys
import time

import boto3
from botocore.config import Config


class ReleaseError(RuntimeError):
    pass


@contextmanager
def release_signals():
    def interrupted(signum, frame):
        raise KeyboardInterrupt("release interrupted")
    old = {s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)


# Read-only DescribeTaskDefinition adds fields that RegisterTaskDefinition rejects.
REGISTER_FIELDS = {
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
    "pidMode", "ipcMode", "proxyConfiguration", "inferenceAccelerators",
    "ephemeralStorage", "runtimePlatform", "enableFaultInjection",
}


def registration(task):
    return {key: deepcopy(value) for key, value in task.items() if key in REGISTER_FIELDS}


def comparable(task):
    result = registration(task)
    for container in result["containerDefinitions"]:
        container.pop("image", None)
    return result


class Release:
    def __init__(self, *, ecs, ecr, cluster, service, repository_uri, baseline,
                 manifest_path, timeout=1200, poll_seconds=5, clock=time.monotonic,
                 sleep=time.sleep):
        if not re.fullmatch(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9/_-]+", repository_uri):
            raise ReleaseError("require a private same-region ECR repository URI")
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in (timeout, poll_seconds)):
            raise ReleaseError("release timing must be positive")
        self.ecs, self.ecr = ecs, ecr
        self.cluster, self.service = cluster, service
        self.repository_uri, self.repository = repository_uri, repository_uri.split("/", 1)[1]
        self.baseline = baseline
        self.manifest_path = Path(manifest_path)
        self.timeout, self.poll_seconds, self.clock, self.sleep = timeout, poll_seconds, clock, sleep
        self.deadline = clock() + timeout
        self.manifest = {"cluster": cluster, "service": service, "outcome": "preflight"}

    def record(self, **values):
        self.manifest.update(values)
        # Write checkpoint before each mutation; no task environment or credentials.
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2) + "\n")
        temporary.replace(self.manifest_path)

    def pause(self):
        if self.clock() >= self.deadline:
            raise ReleaseError("release deadline expired")
        self.sleep(min(self.poll_seconds, self.deadline - self.clock()))

    def service_state(self, *, allow_failed=False):
        response = self.ecs.describe_services(cluster=self.cluster, services=[self.service])
        if response.get("failures") or len(response.get("services", [])) != 1:
            raise ReleaseError("service lookup failed")
        state = response["services"][0]
        if state.get("status") != "ACTIVE" or state.get("desiredCount") not in (0, 1):
            raise ReleaseError("service must be active with desired count zero or one")
        deployment = state.get("deploymentConfiguration", {})
        if deployment.get("minimumHealthyPercent") != 0 or deployment.get("maximumPercent") != 100:
            raise ReleaseError("service does not use approved stop-first percentages")
        if state.get("deploymentController", {}).get("type", "ECS") != "ECS":
            raise ReleaseError("require ECS rolling deployment controller")
        if state.get("schedulingStrategy", "REPLICA") != "REPLICA":
            raise ReleaseError("require replica service")
        if not allow_failed and any(d.get("status") == "PRIMARY" and d.get("rolloutState") == "FAILED"
               for d in state.get("deployments", [])):
            raise ReleaseError("primary deployment failed")
        return state

    def task_definition(self, arn):
        return self.ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]

    def validate_task(self, task):
        containers = task.get("containerDefinitions", [])
        if len(containers) != 1 or containers[0].get("name") != "ingestion":
            raise ReleaseError("expected one ingestion container")
        c = containers[0]
        env = {e["name"]: e["value"] for e in c.get("environment", [])}
        health = c.get("healthCheck", {})
        if (task.get("networkMode") != "awsvpc" or task.get("requiresCompatibilities") != ["FARGATE"]
            or task.get("runtimePlatform") != {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"}
            or c.get("user") != "10001:10001" or c.get("readonlyRootFilesystem") is not True
            or c.get("essential") is not True or c.get("portMappings")
            or c.get("stopTimeout") != 120 or health.get("startPeriod") != 300
            or health.get("command") != ["CMD", "python", "-m", "finbot_ingestion.main", "--health-check"]
            or env.get("RUNTIME_SEC_STARTUP_QUIET_SECONDS") != "150"
            or env.get("SEC_MAX_REQUESTS_PER_SECOND") != "5"
            or any(e.startswith("AWS_ACCESS_KEY") or e.startswith("AWS_SECRET") or e == "AWS_SESSION_TOKEN" for e in env)
            or not task.get("taskRoleArn") or not task.get("executionRoleArn")):
            raise ReleaseError("task definition violates approved runtime contract")
        if not task.get("family") or not c.get("image", "").startswith(self.repository_uri + "@sha256:"):
            raise ReleaseError("task definition must use the configured repository digest")
        return c

    def tasks(self, known=None):
        known = set(known or ())
        # Include desired STOPPED so containers still stopping are not omitted.
        for desired in ("RUNNING", "STOPPED"):
            for page in self.ecs.get_paginator("list_tasks").paginate(
                cluster=self.cluster, serviceName=self.service, desiredStatus=desired):
                known.update(page.get("taskArns", []))
        result = []
        ordered = sorted(known)
        for start in range(0, len(ordered), 100):
            response = self.ecs.describe_tasks(cluster=self.cluster, tasks=ordered[start:start + 100])
            if response.get("failures"):
                raise ReleaseError("cannot prove state of every observed task")
            batch = response.get("tasks", [])
            if {t["taskArn"] for t in batch} != set(ordered[start:start + 100]):
                raise ReleaseError("task description is incomplete")
            result.extend(batch)
        return result

    def quiesce(self):
        observed = {t["taskArn"] for t in self.tasks()}
        self.record(checkpoint="stopping", observed_tasks=sorted(observed))
        self.ecs.update_service(cluster=self.cluster, service=self.service, desiredCount=0)
        consecutive = 0
        while True:
            tasks = self.tasks(observed)
            observed.update(t["taskArn"] for t in tasks)
            state = self.service_state(allow_failed=True)
            stopped = (state["desiredCount"] == 0 and state.get("runningCount") == 0
                and state.get("pendingCount") == 0 and all(t.get("lastStatus") == "STOPPED" for t in tasks))
            consecutive = consecutive + 1 if stopped else 0
            if consecutive >= 2:
                self.record(checkpoint="stopped", observed_tasks=sorted(observed))
                return
            self.pause()

    def await_revision(self, arn, digest, count):
        while True:
            state = self.service_state()
            primary = [d for d in state.get("deployments", []) if d.get("status") == "PRIMARY"]
            tasks = [t for t in self.tasks() if t.get("lastStatus") != "STOPPED"]
            if len(tasks) > 1:
                raise ReleaseError("unexpected overlapping tasks")
            settled = (state.get("taskDefinition") == arn and state["desiredCount"] == count
                and state.get("runningCount") == count and state.get("pendingCount") == 0
                and len(primary) == 1 and primary[0].get("taskDefinition") == arn
                and primary[0].get("rolloutState") == "COMPLETED"
                and len(state.get("deployments", [])) == 1)
            if count == 0 and settled and not tasks:
                return
            if count == 1 and settled and len(tasks) == 1:
                task = tasks[0]
                containers = task.get("containers", [])
                if (task.get("taskDefinitionArn") == arn and task.get("healthStatus") == "HEALTHY"
                    and task.get("lastStatus") == "RUNNING" and len(containers) == 1
                    and containers[0].get("name") == "ingestion" and containers[0].get("imageDigest") == digest
                    and containers[0].get("healthStatus") == "HEALTHY"):
                    return
            self.pause()

    def deploy(self, digest, *, activate=False, activation_approved=False, source_commit=None):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ReleaseError("require resolved image digest")
        if activate and not activation_approved:
            raise ReleaseError("initial activation needs explicit production-readiness approval")
        current = self.service_state(allow_failed=True)
        if current["desiredCount"] and any(d.get("rolloutState") == "FAILED" for d in current.get("deployments", [])):
            raise ReleaseError("stop the failed service before manual recovery")
        previous = current["taskDefinition"]
        old_count = current["desiredCount"]
        baseline = self.task_definition(self.baseline)
        existing = self.task_definition(previous)
        self.validate_task(baseline)
        self.validate_task(existing)
        if comparable(existing) != comparable(baseline):
            raise ReleaseError("current task configuration differs from CDK baseline; reconcile infrastructure first")
        active = [t for t in self.tasks() if t.get("lastStatus") != "STOPPED"]
        if len(active) > 1 or len(current.get("deployments", [])) != 1:
            raise ReleaseError("release requires a settled single-task service")
        response = self.ecr.describe_images(repositoryName=self.repository, imageIds=[{"imageDigest": digest}])
        details = response.get("imageDetails", [])
        if len(details) != 1 or details[0].get("imageDigest") != digest:
            raise ReleaseError("image digest not found")
        if details[0].get("imageManifestMediaType") not in {
            "application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"}:
            raise ReleaseError("require a validated single-platform ARM64 image")
        target_count = 1 if old_count or activate else 0
        # Existing activation cannot be inferred merely from a repository push.
        if target_count and not activation_approved:
            raise ReleaseError("active releases require production-readiness approval")
        args = registration(baseline)
        args["containerDefinitions"][0]["image"] = self.repository_uri + "@" + digest
        self.record(previous_revision=previous, previous_count=old_count, image_digest=digest,
            source_commit=source_commit, checkpoint="registering")
        new = self.ecs.register_task_definition(**args)["taskDefinition"]["taskDefinitionArn"]
        self.record(new_revision=new, checkpoint="registered")
        interrupted = False
        try:
            if old_count:
                interrupted = True
                self.quiesce()
            else:
                # A stopped service can still have a container finishing shutdown.
                interrupted = True
                self.quiesce()
            self.record(checkpoint="starting")
            self.ecs.update_service(cluster=self.cluster, service=self.service,
                taskDefinition=new, desiredCount=target_count)
            self.await_revision(new, digest, target_count)
            self.record(outcome="running" if target_count else "staged", checkpoint="verified")
            return self.manifest
        except BaseException:
            self.record(outcome="failed", checkpoint="restore_required")
            if interrupted:
                # Restoration has a separate finite deadline, including on cancellation.
                self.deadline = self.clock() + self.timeout
                try:
                    self.quiesce()
                    old_digest = existing["containerDefinitions"][0]["image"].split("@", 1)[1]
                    self.record(checkpoint="restoring")
                    self.ecs.update_service(cluster=self.cluster, service=self.service,
                        taskDefinition=previous, desiredCount=old_count)
                    self.await_revision(previous, old_digest, old_count)
                    self.record(outcome="failed_restored", checkpoint="restored")
                except BaseException as recovery_error:
                    # Fail closed; do not launch another task after unproved quiescence.
                    self.record(outcome="failed_restore_unverified", recovery_error_type=type(recovery_error).__name__)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("region", "cluster", "service", "repository-uri", "baseline", "image-digest"):
        parser.add_argument("--" + arg, required=True)
    parser.add_argument("--manifest", default="release-manifest.json")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--activation-approved", action="store_true")
    parser.add_argument("--source-commit")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9/_-]+", args.repository_uri):
        parser.error("invalid private ECR repository URI")
    account, region = args.repository_uri.split(".")[0], args.repository_uri.split(".")[3]
    if region != args.region or not re.fullmatch(r"\d{12}", account):
        parser.error("repository must match the release region")
    sdk = Config(connect_timeout=5, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3})
    session = boto3.Session(region_name=args.region)
    try:
        with release_signals(), closing(session.client("ecs", config=sdk)) as ecs, closing(session.client("ecr", config=sdk)) as ecr:
            runner = Release(ecs=ecs, ecr=ecr, cluster=args.cluster, service=args.service,
                repository_uri=args.repository_uri, baseline=args.baseline, manifest_path=args.manifest, timeout=args.timeout)
            runner.deploy(args.image_digest, activate=args.activate,
                activation_approved=args.activation_approved, source_commit=args.source_commit)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # SDK errors may contain sensitive URLs; type and durable manifest suffice.
        print(f"Release failed: {type(exc).__name__}; inspect {args.manifest}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
