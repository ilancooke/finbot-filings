from dataclasses import replace
from pathlib import Path
import re

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template
from config import InfraConfig
from state_stack import StateStack
from ingestion_stack import IngestionStack


@pytest.fixture(scope="module")
def templates():
    config = InfraConfig.read(Path(__file__).parents[1] / "config.example.json")
    app = App()
    env = Environment(account=config.account, region=config.region)
    state = StateStack(app, "state", config=config, env=env)
    runtime = IngestionStack(app, "runtime", config=config, state=state, env=env)
    app.synth()
    return Template.from_stack(state).to_json(), Template.from_stack(runtime).to_json()


def resources(template, kind):
    return [r for r in template["Resources"].values() if r["Type"] == kind]


def statements(template):
    for policy in resources(template, "AWS::IAM::Policy"):
        yield from policy["Properties"]["PolicyDocument"]["Statement"]


def test_exact_retained_schema(templates):
    state, _ = templates
    tables = resources(state, "AWS::DynamoDB::Table")
    assert len(tables) == 4
    keys = {r["Properties"]["TableName"].rsplit("-", 1)[1]: r for r in tables}
    expected = {"companies": ["cik"], "calendar": ["expected_date", "cik"],
        "filings": ["accession_number"], "artifacts": ["artifact_id"]}
    indexes = []
    for name, table in keys.items():
        p = table["Properties"]
        assert [k["AttributeName"] for k in p["KeySchema"]] == expected[name]
        assert p["BillingMode"] == "PAY_PER_REQUEST" and p["DeletionProtectionEnabled"]
        assert p["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"]
        assert table["DeletionPolicy"] == table["UpdateReplacePolicy"] == "Retain"
        assert "TimeToLiveSpecification" not in p and "StreamSpecification" not in p
        assert all(a["AttributeType"] == "S" for a in p["AttributeDefinitions"])
        for index in p.get("GlobalSecondaryIndexes", []):
            assert index["Projection"] == {"ProjectionType": "KEYS_ONLY"}
            indexes.append((index["IndexName"], [k["AttributeName"] for k in index["KeySchema"]]))
    assert sorted(indexes) == sorted([
        ("EnabledCompanies", ["enabled_marker", "cik"]),
        ("PendingFilingEnumeration", ["pending_work_kind", "pending_work_sort"]),
        ("PendingArtifactWork", ["pending_work_kind", "pending_work_sort"]),
        ("ArtifactsByAccession", ["accession_number", "filename"])])


def test_create_only_data_policies_and_encryption(templates):
    state, runtime = templates
    bucket = resources(state, "AWS::S3::Bucket")[0]
    assert bucket["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"
    assert bucket["Properties"]["BucketEncryption"]
    assert all(bucket["Properties"]["PublicAccessBlockConfiguration"].values())
    policy = resources(state, "AWS::S3::BucketPolicy")[0]["Properties"]["PolicyDocument"]["Statement"]
    assert any(s.get("Condition", {}).get("Null", {}).get("s3:if-none-match") == "true" and s["Effect"] == "Deny" for s in policy)
    assert any(s.get("Condition", {}).get("StringNotEquals", {}).get("s3:if-none-match") == "*" for s in policy)
    assert any(s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false" for s in policy)
    assert resources(state, "AWS::SNS::Topic")[0]["Properties"]["KmsMasterKeyId"]
    queue = resources(state, "AWS::SQS::Queue")[0]
    assert queue["Properties"]["SqsManagedSseEnabled"] and queue["Properties"]["MessageRetentionPeriod"] == 1209600
    for kind in ("AWS::S3::Bucket", "AWS::SNS::Topic", "AWS::SQS::Queue", "AWS::ECR::Repository", "AWS::KMS::Key"):
        assert all(r["DeletionPolicy"] == r["UpdateReplacePolicy"] == "Retain" for r in resources(state, kind))
    for s in statements(runtime):
        ops = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        if "s3:PutObject" in ops and s["Effect"] == "Allow":
            assert s["Condition"]["StringEquals"]["s3:if-none-match"] == "*"
        if s["Effect"] == "Allow":
            assert not set(ops) & {"s3:DeleteObject", "dynamodb:Scan", "dynamodb:DeleteItem", "ecs:RunTask", "sqs:ReceiveMessage"}
        if "iam:PassRole" in ops:
            assert s["Resource"] != "*" and s["Condition"]["StringEquals"]["iam:PassedToService"] == "ecs-tasks.amazonaws.com"


def test_stopped_fargate_and_no_ingress(templates):
    _, runtime = templates
    service = resources(runtime, "AWS::ECS::Service")[0]["Properties"]
    assert service["DesiredCount"] == 0
    assert service["DeploymentConfiguration"]["MinimumHealthyPercent"] == 0
    assert service["DeploymentConfiguration"]["MaximumPercent"] == 100
    task = resources(runtime, "AWS::ECS::TaskDefinition")[0]["Properties"]
    assert task["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    assert task["NetworkMode"] == "awsvpc" and task["RequiresCompatibilities"] == ["FARGATE"]
    c = task["ContainerDefinitions"][0]
    assert c["User"] == "10001:10001" and c["ReadonlyRootFilesystem"]
    assert c["StopTimeout"] == 120 and c["HealthCheck"]["StartPeriod"] == 300
    assert c["MountPoints"] == [{"ContainerPath": "/tmp", "ReadOnly": False, "SourceVolume": "heartbeat"}]
    assert c["LinuxParameters"]["Capabilities"]["Drop"] == ["ALL"]
    assert not c.get("PortMappings")
    env = {e["Name"]: e["Value"] for e in c["Environment"]}
    assert env["RUNTIME_SEC_STARTUP_QUIET_SECONDS"] == "150"
    assert env["RUNTIME_METRICS_ENVIRONMENT"] == "example"
    assert env["CALENDAR_PROVIDER"] == "placeholder"
    assert env["CALENDAR_LOOKAHEAD_DAYS"] == "30"
    assert "@sha256:" in str(c["Image"])
    assert not resources(runtime, "AWS::EC2::SecurityGroupIngress")
    assert all(not r["Properties"].get("SecurityGroupIngress") for r in resources(runtime, "AWS::EC2::SecurityGroup"))
    assert not resources(runtime, "AWS::EC2::NatGateway")
    assert not resources(runtime, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert not resources(runtime, "AWS::ApplicationAutoScaling::ScalableTarget")


def test_yahoo_selection_is_explicit_and_does_not_activate_or_add_resources():
    config = replace(InfraConfig.read(Path(__file__).parents[1] / "config.example.json"), calendar_provider="yahoo")
    app = App()
    env = Environment(account=config.account, region=config.region)
    state = StateStack(app, "yahoo-state", config=config, env=env)
    stack = IngestionStack(app, "yahoo-runtime", config=config, state=state, env=env)
    template = Template.from_stack(stack).to_json()
    container = resources(template, "AWS::ECS::TaskDefinition")[0]["Properties"]["ContainerDefinitions"][0]
    values = {e["Name"]:e["Value"] for e in container["Environment"]}
    assert values["CALENDAR_PROVIDER"] == "yahoo"
    assert values["CALENDAR_PROVIDER_ATTEMPTS"] == "1"
    assert values["CALENDAR_FULL_REFRESH_SECONDS"] == "86400"
    assert values["CALENDAR_NEAR_TERM_REFRESH_SECONDS"] == "0"
    assert values["YAHOO_CACHE_DIR"] == "/tmp/finbot-yahoo"
    assert resources(template, "AWS::ECS::Service")[0]["Properties"]["DesiredCount"] == 0
    assert not any("SECRET" in name or "API_KEY" in name for name in values)


def test_oidc_and_liveness_alarms(templates):
    _, runtime = templates
    trusts = [r["Properties"]["AssumeRolePolicyDocument"]["Statement"] for r in resources(runtime, "AWS::IAM::Role")]
    oidc = [s for group in trusts for s in group if s["Action"] == "sts:AssumeRoleWithWebIdentity"][0]
    assert oidc["Condition"]["StringEquals"]["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert oidc["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"] == "repo:EXAMPLE/finbot-filings:ref:refs/heads/main"
    alarms = [a["Properties"] for a in resources(runtime, "AWS::CloudWatch::Alarm")]
    assert len(alarms) == 11 and not any(a["ActionsEnabled"] for a in alarms)
    liveness = next(a for a in alarms if a.get("MetricName") == "RuntimeHealthy")
    assert liveness["TreatMissingData"] == "breaching"
    assert {d["Name"]: d["Value"] for d in liveness["Dimensions"]} == {"Service": "finbot-ingestion", "Environment": "example"}
    assert any(a.get("MetricName") == "CalendarStale" for a in alarms)


@pytest.mark.parametrize("changes", [{"github_subject": "repo:org/repo:*"}, {"image_digest": "latest"},
    {"account": "123"}, {"region": "invalid"}, {"cpu": 1024, "memory_mib": 512},
    {"monitoring_enabled": "false"}, {"discovery_latency_ms": float("nan")},
    {"publish_error_threshold": 0}, {"github_oidc_provider_arn": "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"}])
def test_invalid_config_fails_without_aws(changes):
    config = InfraConfig.read(Path(__file__).parents[1] / "config.example.json")
    with pytest.raises(ValueError):
        replace(config, **changes)


def test_release_permissions_match_verified_api_scopes(templates):
    _, runtime = templates
    policies = [r for key, r in runtime["Resources"].items()
        if r["Type"] == "AWS::IAM::Policy" and "ReleaseRole" in key]
    assert len(policies) == 1
    actions = {}
    for s in policies[0]["Properties"]["PolicyDocument"]["Statement"]:
        ops = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        for op in ops:
            actions[op] = s
    assert actions["ecr:DescribeImages"]["Resource"] != "*"
    assert actions["ecs:ListTasks"]["Resource"] == "*"
    assert "ecs:cluster" in actions["ecs:ListTasks"]["Condition"]["ArnEquals"]
    assert actions["ecs:RegisterTaskDefinition"]["Resource"].endswith("task-definition/finbot-example-ingestion:*")
    assert "ecs:RunTask" not in actions and "ecs:TagResource" not in actions
