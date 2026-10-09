"""Explicit application wiring; imports never resolve credentials or contact AWS."""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import timedelta
import logging
import os
import signal

from .config import IngestionConfig, ConfigurationError
from .runtime.config import RuntimeConfig
from .runtime.clock import Clock
from .runtime.application import RuntimeApplication
from .observability.health import HealthFile
from .observability.logging import configure_logging


def build_application(stack, environ=None):
    from .aws_config import AWSIOConfig, StorageConfig, MessagingConfig
    from .aws_execution import AWSExecution
    from .execution import BlockingExecution
    from .repositories.dynamodb import (DynamoDBConfig, DynamoDBExecution,
        DynamoDBCompanyRepository, DynamoDBCalendarRepository,
        DynamoDBFilingRepository, DynamoDBArtifactRepository)
    from .repositories.dynamodb.satisfaction import DynamoDBSatisfactionRepository
    from .calendar.config import CalendarConfig
    from .calendar.service import CalendarSyncService
    from .ingestion.config import WorkflowConfig
    from .ingestion.work_control import WorkControl
    from .ingestion.discovery_service import DiscoveryService
    from .ingestion.artifact_downloader import ArtifactDownloader
    from .ingestion.ingestion_worker import IngestionWorker
    from .ingestion.recovery_service import RecoveryService
    from .storage.s3_artifact_store import S3ArtifactStore
    from .messaging.sns_publisher import SNSArtifactEventPublisher
    from .messaging.dead_letter import SQSDeadLetterPublisher
    from .sec.client import SecClient
    from .sec.rate_limiter import SECRateLimiter
    from .scheduler.market_sessions import ExchangeMarketSessions
    from .scheduler.window_policy import WindowPolicy

    runtime = RuntimeConfig.from_env(environ)
    sec_config, db_config = IngestionConfig.from_env(environ), DynamoDBConfig.from_env(environ)
    aws, storage, messaging = AWSIOConfig.from_env(environ), StorageConfig.from_env(environ), MessagingConfig.from_env(environ)
    calendar, workflow = CalendarConfig.from_env(environ), WorkflowConfig.from_env(environ)
    if not 1 <= workflow.max_inflight_artifacts <= 16:
        raise ConfigurationError("runtime artifact worker count must be in [1, 16]")
    if calendar.provider != "placeholder":
        raise ConfigurationError("no live calendar provider is implemented; inject an adapter for offline runtime tests")
    if db_config.region != aws.region or aws.region != messaging.region:
        raise ConfigurationError("AWS regions must agree")
    clock = Clock()
    today = clock.now().date()
    # Rebuild annually through a rolling adapter below, rather than expire a long-running task.
    class RollingSessions:
        def __init__(self):
            self.adapter = None

        def session(self, day):
            if self.adapter is None or not self.adapter.start <= day <= self.adapter.end:
                self.adapter = ExchangeMarketSessions(start=day - timedelta(days=366), end=day + timedelta(days=732))
            return self.adapter.session(day)

    windows = WindowPolicy(RollingSessions(), runtime)
    if windows.lookback_days + max(calendar.near_term_days, windows.forward_days) > db_config.max_calendar_range_days:
        raise ConfigurationError("runtime lookback/near-term range exceeds DynamoDB maximum")
    if calendar.lookahead_days > db_config.max_calendar_range_days:
        raise ConfigurationError("calendar lookahead exceeds DynamoDB maximum")
    # Validate exchange adapter availability before any AWS client construction.
    windows.sessions.session(today)
    db = stack.enter_context(DynamoDBExecution.from_config(db_config))
    executions = [stack.enter_context(AWSExecution.from_config(service, aws)) for service in ("s3", "sns", "sqs")]
    s3, sns, sqs = executions
    sec_execution = stack.enter_context(BlockingExecution(max_workers=1, name="sec"))
    sec = stack.enter_context(SecClient(sec_config, limiter=SECRateLimiter(sec_config.sec_max_requests_per_second)))
    companies, events = DynamoDBCompanyRepository(db), DynamoDBCalendarRepository(db)
    filings, artifacts = DynamoDBFilingRepository(db), DynamoDBArtifactRepository(db)
    control = WorkControl(workflow)
    discovery = DiscoveryService(sec, sec_execution, filings, artifacts, control)
    downloader = ArtifactDownloader(sec, sec_execution, S3ArtifactStore(s3, storage), max_artifact_bytes=storage.max_artifact_bytes)
    worker = IngestionWorker(discovery, downloader, SNSArtifactEventPublisher(sns, messaging),
        SQSDeadLetterPublisher(sqs, messaging), filings, artifacts, control)
    application = RuntimeApplication(config=runtime, clock=clock, windows=windows, worker=worker,
        recovery=RecoveryService(worker), satisfaction=DynamoDBSatisfactionRepository(db, filings),
        calendar_service=CalendarSyncService.with_placeholder(companies, events, calendar))
    for execution in (db, *executions):
        execution.metrics = application.metrics
    return application


async def serve(application):
    loop = asyncio.get_running_loop()
    installed = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, application.request_stop)
            installed.append(sig)
        await application.run()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def main(argv=None, *, builder=build_application):
    parser = argparse.ArgumentParser(description="Single-process SEC ingestion runtime")
    parser.add_argument("--health-check", action="store_true", help="check only the local heartbeat; no AWS calls")
    args = parser.parse_args(argv)
    if args.health_check:
        config = RuntimeConfig.from_env()
        return 0 if HealthFile.check(config.health_path, max_age_seconds=config.heartbeat_seconds * 3) else 1
    try:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
        with ExitStack() as stack:
            application = builder(stack)
            asyncio.run(serve(application))
        return 0
    except Exception as exc:
        logging.getLogger(__name__).error("Ingestion runtime failed", extra={"error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
