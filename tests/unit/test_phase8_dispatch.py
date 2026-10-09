"""Shutdown dispatch boundaries and startup timing, with no network."""
import asyncio
from dataclasses import replace
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.sec.errors import SECRequestStopped
from finbot_ingestion.execution import BlockingExecution
from test_ingestion_transport import make, Response, URL


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True])
def test_quiet_period_rejects_invalid_values(value):
    with pytest.raises(ConfigurationError):
        RuntimeConfig(sec_startup_quiet_seconds=value)


def test_runtime_environment_and_local_default():
    assert RuntimeConfig().sec_startup_quiet_seconds == 0
    config = RuntimeConfig.from_env({"RUNTIME_SEC_STARTUP_QUIET_SECONDS": "150", "RUNTIME_METRICS_ENVIRONMENT": "production"})
    assert config.sec_startup_quiet_seconds == 150 and config.metrics_environment == "production"
    with pytest.raises(ConfigurationError):
        RuntimeConfig(metrics_environment="unsafe space")


@pytest.mark.parametrize("status,headers", [(429, {}), (302, {"Location": "/Archives/next"})])
def test_shutdown_rejects_retry_and_redirect_without_failure(status, headers):
    client, session, clock = make([Response(status=status, headers=headers), Response()])
    if status == 429:
        client._sleep = lambda seconds: client.stop_admissions()
    else:
        original = session.get
        def get(*args, **kwargs):
            result = original(*args, **kwargs)
            client.stop_admissions()
            return result
        session.get = get
    with pytest.raises(SECRequestStopped):
        client.download_document(URL)
    assert len(session.calls) == 1 and not session.closed
    client.close()
    assert session.closed


def test_shutdown_rechecks_after_limiter_wait():
    client, session, _ = make([Response()])
    client.limiter.wait = client.stop_admissions
    with pytest.raises(SECRequestStopped):
        client.download_document(URL)
    assert not session.calls


def test_inflight_finishes_but_waiting_dispatch_is_refused():
    client, session, _ = make([Response()])
    entered, finish = threading.Event(), threading.Event()
    original = session.get
    def get(*args, **kwargs):
        entered.set()
        assert finish.wait(3)
        return original(*args, **kwargs)
    session.get = get
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.download_document, URL)
        assert entered.wait(3)
        second = executor.submit(client.download_document, URL)
        client.stop_admissions()
        assert not session.closed
        finish.set()
        assert first.result(timeout=3).content
        with pytest.raises(SECRequestStopped):
            second.result(timeout=3)
    assert len(session.calls) == 1


def test_stopped_exception_offloading_does_not_count_as_work_error():
    from finbot_ingestion.ingestion.work_control import work_error
    async def scenario():
        with BlockingExecution() as execution:
            with pytest.raises(SECRequestStopped):
                await execution.call(lambda: (_ for _ in ()).throw(SECRequestStopped()))
            assert execution._active == 0
    asyncio.run(scenario())
    assert not work_error(SECRequestStopped())
