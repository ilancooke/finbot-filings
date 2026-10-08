import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from finbot_ingestion.aws_config import AWSIOConfig, MessagingConfig, StorageConfig
from finbot_ingestion.aws_execution import log_sdk_attempt
from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.execution import BlockingExecution
from finbot_ingestion.ingestion.config import WorkflowConfig


def test_isolated_phase4_configuration_has_no_client_or_credential_resolution():
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
def test_invalid_phase4_settings_fail_explicitly(factory):
    with pytest.raises(ConfigurationError):
        factory()


def test_repeated_cancellation_retains_admission_until_blocking_call_finishes():
    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        completed = threading.Event()
        def call():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            completed.set()
        with BlockingExecution(max_workers=1) as execution:
            task = asyncio.create_task(execution.call(call))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(RuntimeError, match="in-flight"):
                execution.close()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert completed.is_set()
            assert await execution.call(lambda: "next") == "next"
    asyncio.run(scenario())


def test_sdk_failed_attempts_and_recovery_emit_logs_without_bodies(caplog):
    with caplog.at_level(logging.INFO):
        log_sdk_attempt(attempts=1, caught_exception=TimeoutError("private body"), operation=SimpleNamespace(name="PutObject"))
        log_sdk_attempt(attempts=2, response=(SimpleNamespace(status_code=200), {}), operation=SimpleNamespace(name="PutObject"))
    assert [r.message for r in caplog.records] == ["AWS request attempt failed", "AWS request recovered"]
    assert caplog.records[0].attempt_number == 1 and "private body" not in caplog.text
