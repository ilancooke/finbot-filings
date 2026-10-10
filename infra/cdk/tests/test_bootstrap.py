"""Validate the customized bootstrap contract without contacting AWS."""
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[3]


def bootstrap():
    return yaml.safe_load((ROOT / "infra/bootstrap/bootstrap-template.yaml").read_text())


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_bootstrap_service_managed_encryption_and_permissions():
    template = bootstrap()
    resources = template["Resources"]
    assert not any(r["Type"].startswith("AWS::KMS::") for r in resources.values())
    bucket = resources["StagingBucket"]
    properties = bucket["Properties"]
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    public_access = properties["PublicAccessBlockConfiguration"]["Fn::If"][1]
    assert all(public_access.values())
    assert template["Parameters"]["PublicAccessBlockConfiguration"]["Default"] == "true"
    statements = list(walk(resources))
    for statement in statements:
        if isinstance(statement, dict) and statement.get("Effect") == "Allow":
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            assert not any(action.lower().startswith("kms:") for action in actions)
    # Preserve the upstream lookup guard rather than widening access during cleanup.
    lookup_deny = resources["LookupRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert any(s["Effect"] == "Deny" and "kms:Decrypt" in s["Action"] for s in lookup_deny)
    bucket_policy = resources["StagingBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    assert any(s["Effect"] == "Deny" and s["Condition"]["Bool"]["aws:SecureTransport"] == "false" for s in bucket_policy)


def test_bootstrap_naming_version_and_cli_compatibility():
    template = bootstrap()
    assert template["Parameters"]["BootstrapVariant"]["Default"] == "Finbot: SSE-S3 v1"
    assert template["Parameters"]["Qualifier"]["Default"] == "hnb659fds"
    assert template["Outputs"]["BootstrapVersion"]["Value"] == "32"
    assert template["Resources"]["CdkBootstrapVersion"]["Properties"]["Value"] == "32"
    assert {"BucketName", "BucketDomainName", "ImageRepositoryName", "BootstrapVersion"} <= template["Outputs"].keys()
    assert "FileAssetKeyArn" not in template["Outputs"]
    expected_roles = {
        "FilePublishingRole", "ImagePublishingRole", "LookupRole",
        "DeploymentActionRole", "CloudFormationExecutionRole"}
    assert {name for name, r in template["Resources"].items() if r["Type"] == "AWS::IAM::Role"} == expected_roles
    # The pinned CLI forwards this input even when using --template. It must not
    # become an unknown CloudFormation parameter or enable a KMS path.
    compatibility = template["Parameters"]["FileAssetsBucketKmsKeyId"]
    assert compatibility["Default"] == "AWS_MANAGED_KEY"
    assert compatibility["AllowedValues"] == ["AWS_MANAGED_KEY"]
    assert not any(node == {"Ref": "FileAssetsBucketKmsKeyId"} for node in walk(template["Resources"]))
    assert len((ROOT / "infra/bootstrap/bootstrap-template.yaml").read_bytes()) < 51200


def test_bootstrap_has_no_dangling_intrinsic_references():
    template = bootstrap()
    names = template["Resources"].keys() | template["Parameters"].keys()
    conditions = template["Conditions"]
    for node in walk(template):
        if not isinstance(node, dict):
            continue
        if "Ref" in node:
            assert node["Ref"] in names or node["Ref"].startswith("AWS::")
        if "Condition" in node and isinstance(node["Condition"], str):
            assert node["Condition"] in conditions
        if "Fn::If" in node:
            assert node["Fn::If"][0] in conditions
        if "Fn::Sub" in node:
            substitution = node["Fn::Sub"]
            text, variables = (substitution, {}) if isinstance(substitution, str) else substitution
            for name in re.findall(r"\$\{([^}]+)\}", text):
                assert name in variables or name.startswith(("AWS::", "!")) or name.split(".")[0] in names
