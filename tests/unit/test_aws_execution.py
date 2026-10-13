"""Sanitized SDK attempt and recovery logging."""

from types import SimpleNamespace
import logging

from finbot_ingestion.aws_execution import log_sdk_attempt


def test_sdk_failed_attempts_and_recovery_emit_logs_without_bodies(caplog):
    with caplog.at_level(logging.INFO):
        log_sdk_attempt(attempts=1, caught_exception=TimeoutError("private body"), operation=SimpleNamespace(name="PutObject"))
        log_sdk_attempt(attempts=2, response=(SimpleNamespace(status_code=200), {}), operation=SimpleNamespace(name="PutObject"))
    assert [r.message for r in caplog.records] == ["AWS request attempt failed", "AWS request recovered"]
    assert caplog.records[0].attempt_number == 1 and "private body" not in caplog.text
