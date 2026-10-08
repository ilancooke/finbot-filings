"""Work classification, local identity locks and bounded checkpoint retries."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging

from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError, HTTPClientError

from finbot_ingestion.repositories.errors import RepositoryBusy, RepositoryConflict, RepositoryDataError
from finbot_ingestion.sec.errors import SECDataError, SECIncompletePackageError, SECNetworkError, SECHTTPError
from finbot_ingestion.storage.errors import StorageConflict, StorageLimitError, StorageNotVisibleError
from finbot_ingestion.messaging.publisher import InvalidPublishResponse
from .retry_policy import RetryPolicy

LOGGER = logging.getLogger(__name__)


class Outcome(str, Enum):
    COMPLETE = "complete"
    DEFERRED = "deferred"
    TERMINAL = "terminal"


def retryable(error):
    if isinstance(error, (SECIncompletePackageError, SECNetworkError, StorageNotVisibleError,
                          InvalidPublishResponse, RepositoryBusy, HTTPClientError,
                          EndpointConnectionError, ConnectionClosedError)):
        return True
    if isinstance(error, SECHTTPError):
        return error.status_code in (403, 404, 429) or error.status_code >= 500
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "")
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500 or status in (409, 429) or code in {
            "Throttling", "ThrottlingException", "ProvisionedThroughputExceededException",
            "RequestLimitExceeded", "RequestThrottled", "TooManyRequestsException",
            "SlowDown", "InternalError", "InternalServerError", "ServiceUnavailable"}
    return False


def work_error(error):
    return retryable(error) or isinstance(error, (SECDataError, SECHTTPError, StorageConflict,
                                                 StorageLimitError, RepositoryConflict, RepositoryDataError))


class IdentityLocks:
    """One registry must be shared by all service callers in this process."""
    def __init__(self):
        self._entries = {}

    @asynccontextmanager
    async def hold(self, key):
        if key not in self._entries:
            self._entries[key] = [asyncio.Lock(), 0]
        entry = self._entries[key]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self._entries[key]


class WorkControl:
    def __init__(self, config, *, sleeper=asyncio.sleep, now=None, random_value=None):
        self.config, self.sleep = config, sleeper
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.random_value = random_value
        self.policy = RetryPolicy(config.max_stage_failures, config.backoff_base_seconds, config.backoff_cap_seconds)

    async def backoff(self, attempt):
        options = {"random_value": self.random_value} if self.random_value is not None else {}
        await self.sleep(self.policy.delay(attempt, **options))

    async def checkpoint(self, action, *, operation, identity):
        """Replay the same logical action, including its original timestamp."""
        for attempt in range(1, self.config.checkpoint_attempts + 1):
            try:
                result = await action()
                if attempt > 1:
                    LOGGER.info("Checkpoint recovered", extra={"operation": operation, "work_id": identity,
                                                               "attempt_number": attempt})
                return result
            except Exception as exc:
                will_retry = retryable(exc) and attempt < self.config.checkpoint_attempts
                LOGGER.warning("Checkpoint failed", extra={"operation": operation, "work_id": identity,
                    "attempt_number": attempt, "error_type": type(exc).__name__, "will_retry": will_retry})
                if not will_retry:
                    raise
                await self.backoff(attempt)

    async def failure(self, repo, identity, stage, error, progress):
        from finbot_ingestion.domain.validation import utc_datetime
        at = utc_datetime(self.now(), "failure timestamp")
        if progress.failure_at is not None:
            at = max(at, progress.failure_at + timedelta(microseconds=1))
        LOGGER.warning("Workflow attempt failed", extra={"operation": stage, "work_id": identity,
            "attempt_number": progress.failures(stage) + 1, "error_type": type(error).__name__,
            "error_message": str(error)[:2048], "will_retry": retryable(error) and
                progress.failures(stage) + 1 < self.config.max_stage_failures})
        await self.checkpoint(lambda: repo.record_stage_failure(identity, stage, str(error) or type(error).__name__, at,
            max_failures=self.config.max_stage_failures, error_type=type(error).__name__, terminal=not retryable(error)),
            operation="record_stage_failure", identity=identity)
