"""AWS I/O, storage, messaging and workflow configuration validation."""

import pytest

from finbot_ingestion.aws_config import AWSIOConfig, MessagingConfig, StorageConfig
from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.ingestion.config import WorkflowConfig


def test_isolated_aws_configuration_has_no_client_or_credential_resolution():
    assert AWSIOConfig.from_env({"AWS_REGION": "us-east-1"}).max_attempts == 3
    assert StorageConfig.from_env({"ARTIFACT_BUCKET": "finbot-artifacts"}).max_artifact_bytes == 64 * 1024 * 1024
    assert WorkflowConfig.from_env({"INGESTION_MAX_STAGE_FAILURES": "4"}).max_stage_failures == 4
    assert MessagingConfig.from_env({"AWS_REGION": "us-east-1",
        "ARTIFACT_READY_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:artifacts",
        "INGESTION_DEAD_LETTER_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/failed"})


@pytest.mark.parametrize("factory", [
    lambda: AWSIOConfig.from_env({}), lambda: AWSIOConfig("us-east-1", read_timeout_seconds=float("nan")),
    lambda: AWSIOConfig("us-east-1", max_attempts=0), lambda: StorageConfig("invalid bucket"),
    lambda: StorageConfig("finbot-artifacts", 0), lambda: StorageConfig("finbot-artifacts", 64 * 1024 * 1024 + 1),
    lambda: StorageConfig.from_env({"ARTIFACT_BUCKET": "finbot-artifacts", "MAX_ARTIFACT_BYTES": "bad"}),
    lambda: WorkflowConfig(max_stage_failures=0), lambda: WorkflowConfig(checkpoint_attempts=True),
    lambda: WorkflowConfig(backoff_cap_seconds=0.5), lambda: WorkflowConfig(recovery_page_size=1001),
    lambda: WorkflowConfig.from_env({"INGESTION_MAX_STAGE_FAILURES": "1.5"}),
    lambda: MessagingConfig("us-east-1", "arn:aws:sns:us-west-2:123456789012:artifacts",
                            "https://sqs.us-east-1.amazonaws.com/123456789012/failed"),
    lambda: MessagingConfig("us-east-1", "arn:aws:sns:us-east-1:123456789012:artifacts.fifo",
                            "https://sqs.us-east-1.amazonaws.com/123456789012/failed"),
    lambda: MessagingConfig("us-east-1", "arn:aws:sns:us-east-1:123456789012:artifacts",
                            "https://untrusted.example/123456789012/failed"),
])
def test_invalid_aws_settings_fail_explicitly(factory):
    with pytest.raises(ConfigurationError):
        factory()
