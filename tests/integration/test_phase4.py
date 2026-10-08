"""Offline durable-boundary replay through real repositories and SDK adapters."""

import asyncio
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import boto3
from botocore.exceptions import ClientError, ReadTimeoutError
import pytest

from test_dynamodb_repositories import db, make_filing, make_artifact, NOW, ACCESSION
from finbot_ingestion.aws_config import AWSIOConfig, StorageConfig, MessagingConfig
from finbot_ingestion.aws_execution import AWSExecution
from finbot_ingestion.domain import Company
from finbot_ingestion.domain.identity import artifact_key
from finbot_ingestion.execution import BlockingExecution
from finbot_ingestion.ingestion.artifact_downloader import ArtifactDownloader
from finbot_ingestion.ingestion.config import WorkflowConfig
from finbot_ingestion.ingestion.discovery_service import DiscoveryService
from finbot_ingestion.ingestion.ingestion_worker import IngestionWorker
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.ingestion.work_control import Outcome, WorkControl
from finbot_ingestion.messaging.dead_letter import SQSDeadLetterPublisher
from finbot_ingestion.messaging.sns_publisher import SNSArtifactEventPublisher
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.errors import SECDataError, SECIncompletePackageError, SECNetworkError
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.sec.urls import filing_index_url
from finbot_ingestion.storage.s3_artifact_store import S3ArtifactStore

RAW = b"\x00SEC\xff\r\noriginal"
BUCKET = "finbot-artifacts"
MESSAGING = MessagingConfig("us-east-1", "arn:aws:sns:us-east-1:123456789012:artifact-ready",
                            "https://sqs.us-east-1.amazonaws.com/123456789012/ingestion-failed")


def aws_error(code, status, operation):
    return ClientError({"Error": {"Code": code, "Message": "injected"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class MemoryS3:
    def __init__(self):
        self.objects, self.calls = {}, []
        self.failures, self.lost = deque(), deque()
        self.clock = NOW + timedelta(seconds=1)

    def api(self, operation, params):
        self.calls.append((operation, deepcopy(params)))
        if self.failures and self.failures[0][0] == operation:
            _, error = self.failures.popleft()
            raise error
        key = (params["Bucket"], params["Key"])
        if operation == "HeadObject":
            if key not in self.objects:
                raise aws_error("404", 404, operation)
            return deepcopy(self.objects[key][1])
        assert operation == "PutObject"
        assert params["IfNoneMatch"] == "*"
        if key in self.objects:
            raise aws_error("PreconditionFailed", 412, operation)
        head = {"Metadata": deepcopy(params["Metadata"]), "ContentLength": len(params["Body"]),
                "LastModified": self.clock, "ContentType": params.get("ContentType", "application/octet-stream")}
        self.objects[key] = (params["Body"], head)
        if self.lost and self.lost[0] == operation:
            self.lost.popleft()
            raise ReadTimeoutError(endpoint_url="https://mock.invalid")
        return {"ETag": '"opaque"'}


class MemoryMessages:
    def __init__(self):
        self.events, self.failures, self.lost = [], deque(), deque()

    def api(self, operation, params):
        if self.failures and self.failures[0][0] == operation:
            _, error = self.failures.popleft()
            raise error
        self.events.append((operation, deepcopy(params)))
        if self.lost and self.lost[0] == operation:
            self.lost.popleft()
            raise ReadTimeoutError(endpoint_url="https://mock.invalid")
        return {"MessageId": f"message-{len(self.events)}"}


class FakeSEC:
    def __init__(self):
        self.filings = [make_filing()]
        self.calls, self.failures = [], deque()

    def _call(self, operation):
        self.calls.append(operation)
        if self.failures and self.failures[0][0] == operation:
            _, error = self.failures.popleft()
            raise error

    def get_company_submissions(self, company):
        self._call("submissions")
        return self.filings

    def get_filing_index(self, filing):
        self._call("index")
        root = Path(__file__).parents[1] / "fixtures" / "sec_package"
        # Fixtures use the baseline accession; rewrite the directory for amendments.
        directory = json.loads((root / "directory.json").read_text())
        if filing.accession_number != ACCESSION:
            directory["directory"]["name"] = directory["directory"]["name"].replace(
                ACCESSION.replace("-", ""), filing.accession_number.replace("-", ""))
            for item in directory["directory"]["item"]:
                item["name"] = item["name"].replace(ACCESSION, filing.accession_number)
        html = (root / "index.html").read_bytes().replace(
            ACCESSION.replace("-", "").encode(), filing.accession_number.replace("-", "").encode())
        if filing.form_type == "8-K/A":
            html = html.replace(b">8-K<", b">8-K/A<")
        return parse_filing_index(filing, html, directory)

    def download_document(self, url, *, max_bytes):
        self._call("download")
        assert len(RAW) <= max_bytes
        return DownloadedDocument(RAW, "application/octet-stream", url)


@pytest.fixture
def system(db, monkeypatch):
    with ExitStack() as stack:
        clients = {service: boto3.client(service, region_name="us-east-1", aws_access_key_id="testing",
                                        aws_secret_access_key="testing") for service in ("s3", "sns", "sqs")}
        s3, messages, sec = MemoryS3(), MemoryMessages(), FakeSEC()
        monkeypatch.setattr(clients["s3"], "_make_api_call", s3.api)
        for service in ("sns", "sqs"):
            monkeypatch.setattr(clients[service], "_make_api_call", messages.api)
        executions = {name: stack.enter_context(AWSExecution(client, AWSIOConfig("us-east-1", max_attempts=1)))
                      for name, client in clients.items()}
        sec_execution = stack.enter_context(BlockingExecution(max_workers=1))
        store = S3ArtifactStore(executions["s3"], StorageConfig(BUCKET))
        async def no_sleep(seconds):
            await asyncio.sleep(0)
        def worker(**changes):
            config = WorkflowConfig(checkpoint_attempts=1, dead_letter_attempts=1, **changes)
            control = WorkControl(config, sleeper=no_sleep, now=lambda: NOW + timedelta(seconds=2), random_value=lambda: 0)
            discovery = DiscoveryService(sec, sec_execution, db.filings, db.artifacts, control)
            return IngestionWorker(discovery, ArtifactDownloader(sec, sec_execution, store,
                max_artifact_bytes=64 * 1024 * 1024), SNSArtifactEventPublisher(executions["sns"], MESSAGING),
                SQSDeadLetterPublisher(executions["sqs"], MESSAGING), db.filings, db.artifacts, control)
        yield SimpleNamespace(db=db, sec=sec, s3=s3, messages=messages, store=store, worker=worker)


async def seed(system, *, complete=True, filename="Primary.htm"):
    filing, child = make_filing(), make_artifact(filename)
    await system.db.filings.create_if_absent(filing)
    if complete:
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, [child],
            primary_document_name=filename, completed_at=NOW)
    else:
        await system.db.artifacts.create_if_absent(child)
    return child


def run(coro):
    return asyncio.run(coro)


def test_full_pipeline_order_bytes_duplicate_and_canonical_alias(system, monkeypatch):
    async def scenario():
        await system.db.filings.create_if_absent(make_filing())
        system.sec.filings = [make_filing(ticker="NEW", discovered_at=NOW + timedelta(days=1))]
        timeline = []
        original = system.db.memory.api
        def database(op, params):
            result = original(op, params)
            if op == "UpdateItem":
                timeline.append("database")
            return result
        original_s3, original_messages = system.s3.api, system.messages.api
        def s3(op, params):
            result = original_s3(op, params)
            if op == "PutObject":
                timeline.append("s3")
            return result
        def messages(op, params):
            assert timeline[-1] == "database"
            timeline.append("sns")
            return original_messages(op, params)
        monkeypatch.setattr(system.db.client, "_make_api_call", database)
        monkeypatch.setattr(system.store.execution.client, "_make_api_call", s3)
        monkeypatch.setattr(system.worker().publisher.execution.client, "_make_api_call", messages)
        w = system.worker()
        await w.discover_company(Company(ticker="NEW", cik="320193", name="Apple"))
        assert len(system.s3.objects) == 4
        events = [json.loads(params["Message"]) for op, params in system.messages.events]
        assert len(events) == 4 and all(e["ticker"] == "AAPL" for e in events)
        assert all(body == RAW for body, _ in system.s3.objects.values())
        assert all(e["stored_at"] == "2026-10-08T20:00:01Z" for e in events)
        assert all(e["schema_version"] == "1.0" and "content" not in e for e in events)
        for index, value in enumerate(timeline):
            if value == "sns":
                assert timeline[index-2:index] == ["s3", "database"]
                assert timeline[index+1] == "database"
        downloads = system.sec.calls.count("download")
        await w.discover_company(Company(ticker="AAPL", cik="320193", name="Apple"))
        await RecoveryService(w, page_size=1).run_pass()
        assert system.sec.calls.count("download") == downloads and len(system.messages.events) == 4
    run(scenario())


@pytest.mark.parametrize("boundary", ["s3_ack", "stored_ack", "published_ack", "sns_ack"])
def test_lost_acknowledgments_repair_without_replacing_or_redownloading(system, boundary):
    async def scenario():
        child = await seed(system)
        if boundary == "s3_ack":
            system.s3.lost.append("PutObject")
        elif boundary == "sns_ack":
            system.messages.lost.append("Publish")
        else:
            original = system.db.memory.api
            target = "s3_uri" if boundary == "stored_ack" else "published_at"
            def lose(op, params):
                result = original(op, params)
                if op == "UpdateItem" and target in params.get("ExpressionAttributeNames", {}).values():
                    system.db.client._make_api_call = original
                    raise ReadTimeoutError(endpoint_url="https://mock.invalid")
                return result
            system.db.client._make_api_call = lose
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.COMPLETE
        durable = await system.db.artifacts.get(child.artifact_id)
        assert durable.stored_at == system.s3.clock and durable.published_at is not None
        assert system.sec.calls.count("download") == 1
        assert sum(op == "PutObject" for op, _ in system.s3.calls) == 1
        events = [params["Message"] for op, params in system.messages.events if op == "Publish"]
        assert len(events) == (2 if boundary == "sns_ack" else 1)
        assert len(set(events)) == 1
    run(scenario())


def test_s3_success_database_down_then_restart_preserves_storage_provenance(system, monkeypatch):
    async def scenario():
        child = await seed(system)
        original = system.db.memory.api
        def fail(op, params):
            if op == "UpdateItem":
                raise ReadTimeoutError(endpoint_url="https://mock.invalid")
            return original(op, params)
        monkeypatch.setattr(system.db.client, "_make_api_call", fail)
        with pytest.raises(ReadTimeoutError):
            await system.worker().process_artifact(child.artifact_id)
        assert len(system.s3.objects) == 1 and not system.messages.events
        first = deepcopy(system.s3.objects)
        monkeypatch.setattr(system.db.client, "_make_api_call", original)
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.COMPLETE
        assert system.sec.calls.count("download") == 1 and system.s3.objects == first
        assert (await system.db.artifacts.get(child.artifact_id)).stored_at == system.s3.clock
    run(scenario())


def test_sns_failure_retries_only_publication(system):
    async def scenario():
        child = await seed(system)
        system.messages.failures.extend([("Publish", aws_error("InternalError", 500, "Publish"))] * 2)
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.COMPLETE
        progress = await system.db.artifacts.get_checkpoint(child.artifact_id)
        assert progress.publication_failures == 2 and progress.acquisition_failures == 0
        assert system.sec.calls.count("download") == 1
    run(scenario())


def test_stage_budgets_are_separate_and_survive_restart(system, monkeypatch):
    async def scenario():
        child = await seed(system)
        system.sec.failures.append(("download", SECNetworkError("temporary")))
        w = system.worker()
        original = w.control.backoff
        async def stop(attempt):
            raise asyncio.CancelledError
        monkeypatch.setattr(w.control, "backoff", stop)
        with pytest.raises(asyncio.CancelledError):
            await w.process_artifact(child.artifact_id)
        assert (await system.db.artifacts.get_checkpoint(child.artifact_id)).acquisition_failures == 1
        system.messages.failures.extend([("Publish", aws_error("InternalError", 500, "Publish"))] * 2)
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.COMPLETE
        progress = await system.db.artifacts.get_checkpoint(child.artifact_id)
        assert progress.acquisition_failures == 1 and progress.publication_failures == 2
        assert progress.terminal_at is None
    run(scenario())


def test_terminal_before_children_and_failed_dead_letter_send_survives_restart(system):
    async def scenario():
        await system.db.filings.create_if_absent(make_filing())
        system.sec.failures.extend([("index", SECIncompletePackageError("not yet complete"))] * 3)
        system.messages.failures.append(("SendMessage", aws_error("InternalError", 500, "SendMessage")))
        with pytest.raises(ClientError):
            await system.worker().process_filing(ACCESSION)
        progress = await system.db.filings.get_checkpoint(ACCESSION)
        assert progress.enumeration_failures == 3 and progress.pending_dead_letter
        assert not system.db.memory.tables["artifacts"]
        assert not (await system.db.filings.list_pending()).items
        assert (await system.db.filings.list_pending(kind="DEAD_LETTER")).items
        await RecoveryService(system.worker()).run_pass()
        progress = await system.db.filings.get_checkpoint(ACCESSION)
        assert progress.dead_lettered_at is not None
        assert not (await system.db.filings.list_pending(kind="DEAD_LETTER")).items
        before = len(system.sec.calls)
        await system.worker().process_filing(ACCESSION)
        assert len(system.sec.calls) == before
    run(scenario())


@pytest.mark.parametrize("stage", ["ACQUIRE", "PUBLISH"])
def test_terminal_artifact_retains_facts_and_stable_dlq_duplicates(system, stage):
    async def scenario():
        child = await seed(system)
        if stage == "ACQUIRE":
            system.sec.failures.extend([("download", SECNetworkError("down"))] * 3)
        else:
            system.messages.failures.extend([("Publish", aws_error("InternalError", 500, "Publish"))] * 3)
        system.messages.lost.append("SendMessage")
        with pytest.raises(ReadTimeoutError):
            await system.worker().process_artifact(child.artifact_id)
        progress = await system.db.artifacts.get_checkpoint(child.artifact_id)
        assert progress.terminal_stage == stage and progress.pending_dead_letter
        await RecoveryService(system.worker(), page_size=1).run_pass()
        messages = [params["MessageBody"] for op, params in system.messages.events if op == "SendMessage"]
        assert len(messages) == 2 and messages[0] == messages[1]
        source = await system.db.artifacts.get(child.artifact_id)
        assert (source.stored_at is not None) == (stage == "PUBLISH")
        assert source.published_at is None
        assert not (await system.db.artifacts.list_pending("DEAD_LETTER")).items
    run(scenario())


def test_partial_children_do_not_acquire_before_parent_completion(system):
    async def scenario():
        child = await seed(system, complete=False)
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.DEFERRED
        assert not system.sec.calls and not system.s3.calls
        system.sec.failures.append(("index", SECDataError("malformed package")))
        assert await system.worker().process_filing(ACCESSION) == Outcome.TERMINAL
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.DEFERRED
        assert system.sec.calls == ["index"]
        assert len(system.messages.events) == 1
    run(scenario())


def test_failed_partial_creation_restarts_enumeration_and_acquires_all_children(system):
    async def scenario():
        await system.db.filings.create_if_absent(make_filing())
        system.db.memory.fail_put = make_artifact("Slides.PDF").artifact_id
        w = system.worker()
        async def stop(attempt):
            raise asyncio.CancelledError
        w.control.backoff = stop
        with pytest.raises(asyncio.CancelledError):
            await w.process_filing(ACCESSION)
        assert len(system.db.memory.tables["artifacts"]) == 1
        assert (await system.db.filings.get_checkpoint(ACCESSION)).enumeration_completed_at is None
        system.db.memory.fail_put = None
        await RecoveryService(system.worker(), page_size=1).run_pass()
        assert len(system.s3.objects) == 4 and len(system.messages.events) == 4
    run(scenario())


def test_amendments_have_independent_raw_objects_and_events(system):
    async def scenario():
        amendment = "0000320193-26-000002"
        system.sec.filings.append(make_filing(accession_number=amendment, form_type="8-K/A",
            filing_index_url=filing_index_url("320193", amendment)))
        await system.worker().discover_company(Company(ticker="AAPL", cik="320193", name="Apple"))
        assert len(system.s3.objects) == 8
        identities = [json.loads(p["Message"])["artifact_id"] for _, p in system.messages.events]
        assert len(set(identities)) == 8
    run(scenario())


def test_delayed_gsi_visibility_requires_later_complete_pass(system):
    async def scenario():
        await seed(system)
        system.db.memory.index_snapshots["PendingArtifactWork"] = []
        assert (await RecoveryService(system.worker()).run_pass()).candidates == 0
        assert not system.s3.objects
        del system.db.memory.index_snapshots["PendingArtifactWork"]
        assert (await RecoveryService(system.worker()).run_pass()).complete == 1
    run(scenario())


def test_duplicate_concurrent_calls_publish_once_and_release_identity_locks(system):
    async def scenario():
        child = await seed(system)
        w = system.worker()
        assert await asyncio.gather(*(w.process_artifact(child.artifact_id) for _ in range(10))) == [Outcome.COMPLETE] * 10
        assert system.sec.calls == ["download"] and len(system.messages.events) == 1
        assert not w.locks._entries
    run(scenario())


def test_permission_failures_are_visible_and_do_not_invent_absence_or_terminal_work(system):
    async def scenario():
        child = await seed(system)
        system.s3.failures.append(("HeadObject", aws_error("AccessDenied", 403, "HeadObject")))
        with pytest.raises(ClientError):
            await system.worker().process_artifact(child.artifact_id)
        assert not system.sec.calls and not system.messages.events
        assert (await system.db.artifacts.get_checkpoint(child.artifact_id)).terminal_at is None
    run(scenario())


def test_submissions_failure_has_no_success_watermark_or_invented_filing(system):
    async def scenario():
        system.sec.failures.append(("submissions", SECDataError("bad parallel arrays")))
        with pytest.raises(SECDataError):
            await system.worker().discover_company(Company(ticker="AAPL", cik="320193", name="Apple"))
        assert not system.db.memory.tables["filings"] and not system.messages.events
    run(scenario())


def test_recovery_follows_empty_candidate_pages_and_recovers_old_disabled_company_work(system):
    from finbot_ingestion.repositories.dynamodb.serialization import encode
    async def scenario():
        child = await seed(system)
        await system.db.companies.upsert(Company(ticker="AAPL", cik="320193", name="Apple", enabled=False))
        # Move the original discovery sort back years; no recovery-age filter exists.
        row = next(iter(system.db.memory.tables["artifacts"].values()))
        row["discovered_at"] = "2000-01-01T00:00:00.000000Z"
        row["pending_work_sort"] = row["discovered_at"] + "/" + child.artifact_id
        # First query is pending filings. Second is an empty artifact page with a cursor.
        ghost = {"artifact_id": "0000320193-26-000001/Absent.htm", "pending_work_kind": "ACQUIRE",
                 "pending_work_sort": "1999-01-01T00:00:00.000000Z/0000320193-26-000001/Absent.htm"}
        system.db.memory.query_responses.extend([{"Items": []}, {"Items": [], "LastEvaluatedKey": encode(ghost)}])
        summary = await RecoveryService(system.worker(), page_size=1).run_pass()
        assert summary.complete == 1 and (await system.db.artifacts.get(child.artifact_id)).published_at is not None
        queries = [params for op, params in system.db.memory.calls if op == "Query"]
        assert any("ExclusiveStartKey" in query for query in queries)
        assert not any("FilterExpression" in query for query in queries)
    run(scenario())


def test_bad_oldest_candidate_does_not_starve_later_work(system):
    async def scenario():
        child = await seed(system)
        later = make_artifact("release.htm", discovered_at=NOW + timedelta(seconds=1))
        await system.db.artifacts.create_if_absent(later)
        system.sec.failures.extend([("download", SECNetworkError("down"))] * 3)
        system.messages.failures.extend([("SendMessage", aws_error("InternalError", 500, "SendMessage"))] * 2)
        summary = await RecoveryService(system.worker(), page_size=1).run_pass()
        assert summary.errors == 2 and summary.complete == 1
        assert (await system.db.artifacts.get(later.artifact_id)).published_at is not None
        assert (await system.db.artifacts.get_checkpoint(child.artifact_id)).pending_dead_letter
    run(scenario())


def test_lost_dead_letter_checkpoint_does_not_repeat_successfully_committed_send(system, monkeypatch):
    async def scenario():
        child = await seed(system)
        system.sec.failures.append(("download", SECDataError("unsafe")))
        original = system.db.memory.api
        def lose(op, params):
            result = original(op, params)
            if op == "UpdateItem" and "dead_lettered_at" in params.get("ExpressionAttributeNames", {}).values():
                monkeypatch.setattr(system.db.client, "_make_api_call", original)
                raise ReadTimeoutError(endpoint_url="https://mock.invalid")
            return result
        monkeypatch.setattr(system.db.client, "_make_api_call", lose)
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.TERMINAL
        assert len(system.messages.events) == 1
        await RecoveryService(system.worker()).run_pass()
        assert len(system.messages.events) == 1
    run(scenario())


def test_conditional_409_with_no_object_retries_without_overwrite(system):
    async def scenario():
        child = await seed(system)
        system.s3.failures.append(("PutObject", aws_error("ConditionalRequestConflict", 409, "PutObject")))
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.COMPLETE
        assert len(system.s3.objects) == 1
        assert all(params["IfNoneMatch"] == "*" for op, params in system.s3.calls if op == "PutObject")
        assert (await system.db.artifacts.get_checkpoint(child.artifact_id)).acquisition_failures == 1
    run(scenario())


def test_full_sec_transport_to_event_replay_under_shared_budget(system):
    from finbot_ingestion.config import IngestionConfig
    from finbot_ingestion.sec.client import SecClient
    from finbot_ingestion.sec.rate_limiter import SECRateLimiter
    from finbot_ingestion.sec.urls import company_submissions_url, accession_index_json_url
    class Response:
        def __init__(self, content, *, headers=None):
            self.content, self.headers, self.status_code, self.closed = content, headers or {}, 200, False
        def iter_content(self, chunk_size):
            yield self.content[:3]
            yield self.content[3:]
        def close(self):
            self.closed = True
    class Replay:
        def __init__(self, routes):
            self.headers, self.routes, self.time, self.starts = {}, routes, 0.0, []
        def mount(self, *args):
            pass
        def get(self, url, **kwargs):
            self.starts.append(self.time)
            assert kwargs["allow_redirects"] is False
            return self.routes[url]
        def sleep(self, seconds):
            self.time += seconds
        def close(self):
            pass
    async def scenario():
        root = Path(__file__).parents[1] / "fixtures" / "sec_package"
        submissions = {"cik": 320193, "filings": {"recent": {"accessionNumber": [ACCESSION],
            "form": ["8-K"], "acceptanceDateTime": ["2026-10-08T19:59:50Z"], "primaryDocument": [None]}}}
        routes = {company_submissions_url("320193"): Response(json.dumps(submissions).encode()),
            make_filing().filing_index_url: Response((root / "index.html").read_bytes()),
            accession_index_json_url("320193", ACCESSION): Response((root / "directory.json").read_bytes())}
        for name in ("Primary.htm", "Slides.PDF", "release.htm", "graphic.jpg"):
            routes[make_artifact(name).sec_url] = Response(RAW, headers={"Content-Type": "application/octet-stream"})
        replay = Replay(routes)
        limiter = SECRateLimiter(clock=lambda: replay.time, sleeper=replay.sleep)
        with SecClient(IngestionConfig("Finbot owner@example.com"), limiter=limiter, session=replay,
                       sleeper=replay.sleep, now=lambda: NOW) as client:
            w = system.worker()
            w.discovery.sec, w.downloader.sec = client, client
            await w.discover_company(Company(ticker="AAPL", cik="320193", name="Apple"))
        assert len(replay.starts) == 7 and len(system.messages.events) == 4
        assert all(sum(t <= x <= t + 1 for x in replay.starts) <= 5 for t in replay.starts)
        assert all(response.closed for response in routes.values())
    run(scenario())


def test_unicode_filename_and_uri_escaping_preserve_raw_identity(system):
    from urllib.parse import urlsplit, unquote
    async def scenario():
        child = await seed(system, filename="résumé #?.PDF")
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.COMPLETE
        stored = await system.db.artifacts.get(child.artifact_id)
        assert stored.filename == "résumé #?.PDF"
        uri = urlsplit(stored.s3_uri)
        assert not uri.query and not uri.fragment
        assert unquote(uri.path.lstrip("/")) == artifact_key(child.company_cik, child.accession_number, child.filename)
        assert all(value.isascii() for _, head in system.s3.objects.values() for value in head["Metadata"].values())
    run(scenario())


def test_lost_enumeration_commit_ack_recovers_every_known_child(system, monkeypatch):
    async def scenario():
        await system.db.filings.create_if_absent(make_filing())
        original = system.db.memory.api
        def lose(op, params):
            result = original(op, params)
            if op == "UpdateItem" and "enumeration_completed_at" in params.get("ExpressionAttributeNames", {}).values():
                monkeypatch.setattr(system.db.client, "_make_api_call", original)
                raise ReadTimeoutError(endpoint_url="https://mock.invalid")
            return result
        monkeypatch.setattr(system.db.client, "_make_api_call", lose)
        await RecoveryService(system.worker(), page_size=1).run_pass()
        assert len(system.s3.objects) == 4 and len(system.messages.events) == 4
        progress = await system.db.filings.get_checkpoint(ACCESSION)
        assert progress.enumeration_completed_at is not None and progress.enumeration_failures == 0
    run(scenario())


def test_existing_unverifiable_s3_object_terminalizes_without_replacing_bytes(system):
    async def scenario():
        child = await seed(system)
        key = artifact_key(child.company_cik, child.accession_number, child.filename)
        system.s3.objects[(BUCKET, key)] = (RAW, {"Metadata": {}, "ContentLength": len(RAW), "LastModified": NOW})
        original = deepcopy(system.s3.objects)
        assert await system.worker().process_artifact(child.artifact_id) == Outcome.TERMINAL
        assert system.s3.objects == original and not system.sec.calls
        assert all(op != "PutObject" for op, _ in system.s3.calls)
        assert (await system.db.artifacts.get_checkpoint(child.artifact_id)).acquisition_failures == 1
    run(scenario())


def test_whole_artifact_workflow_admission_bounds_retained_bytes_through_publication(system, monkeypatch):
    async def scenario():
        filing = make_filing()
        children = tuple(make_artifact(name) for name in ("Primary.htm", "release.htm", "Slides.PDF", "graphic.jpg"))
        await system.db.filings.create_if_absent(filing)
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, children,
            primary_document_name="Primary.htm", completed_at=NOW)
        w = system.worker(max_inflight_artifacts=1)
        active, maximum = 0, 0
        acquire, publish = w.downloader.acquire, w.publisher.publish_artifact_ready
        async def observe_acquire(child):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            return await acquire(child)
        async def observe_publish(event):
            nonlocal active
            assert active == 1
            await publish(event)
            active -= 1
        monkeypatch.setattr(w.downloader, "acquire", observe_acquire)
        monkeypatch.setattr(w.publisher, "publish_artifact_ready", observe_publish)
        results = await asyncio.gather(*(w.process_artifact(child.artifact_id) for child in children))
        assert results == [Outcome.COMPLETE] * 4 and maximum == 1 and active == 0
    run(scenario())
