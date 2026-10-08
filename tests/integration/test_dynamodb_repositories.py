"""Real SDK paginators/exceptions over a deterministic, mocked AWS API boundary."""

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import boto3
from botocore.exceptions import ReadTimeoutError
import pytest

from finbot_ingestion.domain import Artifact, Company, ExpectedEarningsEvent, Filing
from finbot_ingestion.repositories.dynamodb import (
    DynamoDBArtifactRepository, DynamoDBCalendarRepository, DynamoDBCompanyRepository,
    DynamoDBConfig, DynamoDBExecution, DynamoDBFilingRepository,
)
from finbot_ingestion.repositories.dynamodb.serialization import decode, encode
from finbot_ingestion.repositories.errors import RepositoryBusy, RepositoryConflict, RepositoryDataError, RepositoryNotFound
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.sec.urls import document_url, filing_index_url

NOW = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
ACCESSION = "0000320193-26-000001"
CONFIG = DynamoDBConfig("us-east-1", "companies", "calendar", "filings", "artifacts")


def make_filing(**changes):
    return replace(Filing(accession_number=ACCESSION, company_cik="320193", ticker="AAPL",
                          form_type="8-K", filed_at=NOW - timedelta(seconds=10), discovered_at=NOW,
                          filing_index_url=filing_index_url("320193", ACCESSION)), **changes)


def make_artifact(filename="Primary.htm", **changes):
    return replace(Artifact(accession_number=ACCESSION, company_cik="320193", ticker="AAPL",
                            form_type="8-K", filename=filename, discovered_at=NOW,
                            sec_url=document_url("320193", ACCESSION, filename)), **changes)


class MemoryAWS:
    """Small DynamoDB API fake with condition evaluation and atomic writes.

    It uses fixed table/index contracts, real SDK AttributeValues, and real SDK
    paginators; scripted responses model effects an in-memory store cannot.
    """
    keys = {"companies": ("cik",), "calendar": ("expected_date", "cik"),
            "filings": ("accession_number",), "artifacts": ("artifact_id",)}
    indexes = {"EnabledCompanies": ("enabled_marker", "cik"),
               "PendingFilingEnumeration": ("pending_work_kind", "pending_work_sort"),
               "PendingArtifactWork": ("pending_work_kind", "pending_work_sort"),
               "ArtifactsByAccession": ("accession_number", "filename")}

    def __init__(self, client):
        self.client, self.tables, self.calls = client, {name: {} for name in self.keys}, []
        self.lock = threading.RLock()
        self.lost = deque()
        self.query_responses = deque()
        self.index_snapshots = {}
        self.fail_put = None
        self.before_update = None
        self.query_cap = 1000

    def key(self, table, item):
        return tuple(item[name] for name in self.keys[table])

    def condition(self, expression, item, names, values):
        if " OR " in expression:
            return any(self.condition(part, item, names, values) for part in expression.split(" OR "))
        if " AND " in expression:
            return all(self.condition(part, item, names, values) for part in expression.split(" AND "))
        if expression.startswith("attribute_not_exists("):
            return names[expression[21:-1]] not in item
        if expression.startswith("attribute_exists("):
            return names[expression[17:-1]] in item
        field, op, value = expression.split()
        actual, expected = item.get(names[field]), values[value]
        return actual is not None and ({"=": lambda: actual == expected,
                                      "<": lambda: actual < expected}[op]())

    def api(self, operation, params):
        with self.lock:
            self.calls.append((operation, deepcopy(params)))
            table = params["TableName"]
            rows = self.tables[table]
            if operation == "GetItem":
                assert params["ConsistentRead"] is True
                item = rows.get(self.key(table, decode(params["Key"])))
                return {} if item is None else {"Item": encode(deepcopy(item))}
            if operation in ("PutItem", "UpdateItem"):
                proposed = decode(params.get("Item", params.get("Key")))
                key = self.key(table, proposed)
                item = rows.get(key, {})
                if operation == "PutItem" and self.fail_put is not None and self.fail_put == proposed.get("artifact_id"):
                    raise self.client.exceptions.InternalServerError({"Error": {
                        "Code": "InternalServerError", "Message": "injected"}}, operation)
                if operation == "UpdateItem" and self.before_update is not None:
                    hook, self.before_update = self.before_update, None
                    hook(item)
                names = params.get("ExpressionAttributeNames", {})
                values = decode(params.get("ExpressionAttributeValues", {}))
                if "ConditionExpression" in params and not self.condition(params["ConditionExpression"], item, names, values):
                    raise self.client.exceptions.ConditionalCheckFailedException({"Error": {
                        "Code": "ConditionalCheckFailedException", "Message": "injected"}}, operation)
                if operation == "PutItem":
                    rows[key] = deepcopy(proposed)
                    response = {}
                else:
                    updated = {**proposed, **item}
                    parts = params["UpdateExpression"][4:].split(" REMOVE ")
                    for assignment in parts[0].split(", "):
                        field, value = assignment.split(" = ")
                        updated[names[field]] = values[value]
                    if len(parts) > 1:
                        for field in parts[1].split(", "):
                            updated.pop(names[field], None)
                    rows[key] = updated
                    response = {"Attributes": encode(deepcopy(updated))}
                if self.lost and self.lost[0] == operation:
                    self.lost.popleft()
                    raise ReadTimeoutError(endpoint_url="https://mock.invalid")
                return response
            if operation == "Query":
                if self.query_responses:
                    return self.query_responses.popleft()
                index = params.get("IndexName")
                if index:
                    assert "ConsistentRead" not in params
                    partition, sort = self.indexes[index]
                else:
                    assert params["ConsistentRead"] is True
                    partition, sort = self.keys[table]
                value = decode(params["ExpressionAttributeValues"])[":pk"]
                candidates = self.index_snapshots.get(index, rows.values())
                candidates = [row for row in candidates if row.get(partition) == value and sort in row]
                order = lambda row: (row[sort], self.key(table, row))
                candidates.sort(key=order)
                if "ExclusiveStartKey" in params:
                    start = decode(params["ExclusiveStartKey"])
                    candidates = [row for row in candidates if order(row) > order(start)]
                limit = min(params["Limit"], self.query_cap)
                selected = candidates[:limit]
                key_names = set(self.keys[table]) | {partition, sort}
                output = [{name: row[name] for name in key_names} if index else row for row in selected]
                response = {"Items": [encode(deepcopy(row)) for row in output], "Count": len(output)}
                if len(candidates) > limit:
                    response["LastEvaluatedKey"] = encode({name: selected[-1][name] for name in key_names})
                return response
            raise AssertionError(f"unexpected AWS operation: {operation}")


@pytest.fixture
def db(monkeypatch):
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing",
                          aws_secret_access_key="testing")
    memory = MemoryAWS(client)
    monkeypatch.setattr(client, "_make_api_call", memory.api)
    with DynamoDBExecution(client, CONFIG) as execution:
        yield SimpleNamespace(memory=memory, client=client, execution=execution,
            filings=DynamoDBFilingRepository(execution), artifacts=DynamoDBArtifactRepository(execution),
            companies=DynamoDBCompanyRepository(execution), calendar=DynamoDBCalendarRepository(execution))


def run(coro):
    return asyncio.run(coro)


def test_create_duplicate_and_conflict_preserve_provenance(db):
    async def scenario():
        filing, artifact = make_filing(), make_artifact()
        assert await db.filings.create_if_absent(filing)
        assert await db.artifacts.create_if_absent(artifact)
        assert not await db.filings.create_if_absent(replace(filing, ticker="NEW", discovered_at=NOW + timedelta(days=1), primary_document_name="Primary.htm"))
        assert not await db.artifacts.create_if_absent(replace(artifact, ticker="NEW", discovered_at=NOW + timedelta(days=1), document_type="8-K"))
        assert await db.filings.get(ACCESSION) == filing
        assert await db.artifacts.get(artifact.artifact_id) == artifact
        for changes in ({"company_cik": "1"}, {"filed_at": NOW}, {"form_type": "10-K"}, {"filing_index_url": "https://different.invalid"}):
            with pytest.raises(RepositoryConflict):
                await db.filings.create_if_absent(replace(filing, **changes))
        with pytest.raises(RepositoryConflict):
            await db.artifacts.create_if_absent(replace(artifact, sec_url="https://different.invalid"))
    run(scenario())


def test_competing_creators_and_case_sensitive_names(db):
    async def scenario():
        results = await asyncio.gather(*(db.filings.create_if_absent(make_filing()) for _ in range(10)))
        assert results.count(True) == 1
        for name in ("Primary.htm", "primary.htm"):
            await db.artifacts.create_if_absent(make_artifact(name))
        assert len(db.memory.tables["artifacts"]) == 2
    run(scenario())


@pytest.mark.parametrize("entity", ["filings", "artifacts"])
def test_lost_create_acknowledgment_is_safe(db, entity):
    async def scenario():
        repo = getattr(db, entity)
        model = make_filing() if entity == "filings" else make_artifact()
        db.memory.lost.append("PutItem")
        with pytest.raises(ReadTimeoutError):
            await repo.create_if_absent(model)
        assert not await repo.create_if_absent(model)
        assert len(db.memory.tables[entity]) == 1
    run(scenario())


def test_storage_publication_order_and_idempotency(db):
    async def scenario():
        artifact = make_artifact()
        await db.artifacts.create_if_absent(artifact)
        with pytest.raises(RepositoryConflict):
            await db.artifacts.mark_published(artifact.artifact_id, NOW)
        uri = "s3://artifacts/" + artifact.artifact_id
        await db.artifacts.mark_stored(artifact.artifact_id, uri, NOW, "text/html", 50)
        await db.artifacts.mark_stored(artifact.artifact_id, uri, NOW + timedelta(hours=1), "text/html", 50)
        assert (await db.artifacts.get(artifact.artifact_id)).stored_at == NOW
        assert (await db.artifacts.list_pending("ACQUIRE")).items == ()
        assert len((await db.artifacts.list_pending("PUBLISH")).items) == 1
        with pytest.raises(RepositoryConflict):
            await db.artifacts.mark_stored(artifact.artifact_id, uri + "different", NOW, "text/html", 50)
        await db.artifacts.mark_published(artifact.artifact_id, NOW)
        await db.artifacts.mark_published(artifact.artifact_id, NOW + timedelta(days=1))
        assert (await db.artifacts.get(artifact.artifact_id)).published_at == NOW
        assert (await db.artifacts.list_pending("PUBLISH")).items == ()
        assert "pending_work_kind" not in next(iter(db.memory.tables["artifacts"].values()))
    run(scenario())


@pytest.mark.parametrize("operation", ["store", "publish", "failure", "enumeration"])
def test_lost_update_acknowledgment_preserves_first_commit(db, operation):
    async def scenario():
        artifact = make_artifact()
        await db.filings.create_if_absent(make_filing())
        await db.artifacts.create_if_absent(artifact)
        uri = "s3://artifacts/" + artifact.artifact_id
        if operation == "publish":
            await db.artifacts.mark_stored(artifact.artifact_id, uri, NOW, None, None)
        def action():
            if operation == "store":
                return db.artifacts.mark_stored(artifact.artifact_id, uri, NOW, None, None)
            if operation == "publish":
                return db.artifacts.mark_published(artifact.artifact_id, NOW)
            if operation == "failure":
                return db.artifacts.record_failure(artifact.artifact_id, "timeout", NOW)
            return db.filings.mark_enumerated(ACCESSION, primary_document_name="Primary.htm", artifact_count=1, completed_at=NOW)
        db.memory.lost.append("UpdateItem")
        with pytest.raises(ReadTimeoutError):
            await action()
        before = deepcopy(db.memory.tables)
        await action()
        assert db.memory.tables == before
    run(scenario())


@pytest.mark.parametrize("operation", ["store", "publish", "failure", "enumeration"])
def test_update_missing_record_does_not_upsert(db, operation):
    async def scenario():
        aid = make_artifact().artifact_id
        with pytest.raises(RepositoryNotFound):
            if operation == "store":
                await db.artifacts.mark_stored(aid, "s3://artifacts/key", NOW, None, None)
            elif operation == "publish":
                await db.artifacts.mark_published(aid, NOW)
            elif operation == "failure":
                await db.artifacts.record_failure(aid, "oops", NOW)
            else:
                await db.filings.mark_enumerated(ACCESSION, primary_document_name="Primary.htm", artifact_count=1, completed_at=NOW)
        assert not any(db.memory.tables.values())
    run(scenario())


def test_failure_replay_old_attempt_and_cas_contention(db):
    async def scenario():
        aid = make_artifact().artifact_id
        await db.artifacts.create_if_absent(make_artifact())
        await db.artifacts.record_failure(aid, "first", NOW)
        await db.artifacts.record_failure(aid, "first", NOW)
        await db.artifacts.record_failure(aid, "older", NOW - timedelta(days=1))
        assert (await db.artifacts.get(aid)).retry_count == 1
        with pytest.raises(RepositoryConflict):
            await db.artifacts.record_failure(aid, "different", NOW)
        def compete(item):
            item["revision"] += 1
        db.memory.before_update = compete
        await db.artifacts.record_failure(aid, "second", NOW + timedelta(seconds=1))
        assert (await db.artifacts.get(aid)).retry_count == 2
        await db.artifacts.record_failure(aid, "large" * 1000, NOW + timedelta(seconds=2))
        assert len((await db.artifacts.get(aid)).last_error) == 2048
    run(scenario())


def test_partial_snapshot_restart_and_first_completion(db):
    async def scenario():
        filing = make_filing()
        children = tuple(make_artifact(name) for name in ("Primary.htm", "release.htm", "Slides.PDF"))
        checkpoint = PackageCheckpoint(db.filings, db.artifacts)
        await db.filings.create_if_absent(filing)
        db.memory.fail_put = children[1].artifact_id
        with pytest.raises(db.client.exceptions.InternalServerError):
            await checkpoint.persist(filing, children, primary_document_name="Primary.htm", completed_at=NOW)
        assert len(db.memory.tables["artifacts"]) == 1
        assert (await db.filings.get_checkpoint(ACCESSION)).enumeration_completed_at is None
        assert (await db.filings.list_pending()).items == (filing,)
        # New repository instances simulate process restart; no in-memory progress.
        restarted = PackageCheckpoint(DynamoDBFilingRepository(db.execution), DynamoDBArtifactRepository(db.execution))
        db.memory.fail_put = None
        assert not await db.filings.create_if_absent(replace(filing, discovered_at=NOW + timedelta(days=2)))
        await restarted.persist(filing, children, primary_document_name="Primary.htm", completed_at=NOW)
        assert len(db.memory.tables["artifacts"]) == 3
        progress = await db.filings.get_checkpoint(ACCESSION)
        assert progress.enumerated_artifact_count == 3
        assert progress.resolved_primary_document_name == "Primary.htm"
        assert (await db.filings.get(ACCESSION)).primary_document_name is None
        assert (await db.filings.list_pending()).items == ()
        await restarted.persist(filing, (*children, make_artifact("later.xml")), primary_document_name="Primary.htm", completed_at=NOW + timedelta(days=2))
        assert await db.filings.get_checkpoint(ACCESSION) == progress
        assert len((await db.artifacts.list_pending("ACQUIRE")).items) == 4
    run(scenario())


def test_more_than_transaction_limit_and_amendment(db):
    async def scenario():
        filing = make_filing()
        await db.filings.create_if_absent(filing)
        children = [make_artifact("Primary.htm")] + [make_artifact(f"exhibit-{i}.xml") for i in range(105)]
        await PackageCheckpoint(db.filings, db.artifacts).persist(filing, children, primary_document_name="Primary.htm", completed_at=NOW)
        amendment = make_filing(accession_number="0000320193-26-000002", form_type="8-K/A")
        await db.filings.create_if_absent(amendment)
        assert len(db.memory.tables["artifacts"]) == 106
        assert (await db.filings.list_pending()).items == (amendment,)
        assert not {op for op, _ in db.memory.calls} & {"TransactWriteItems", "BatchWriteItem", "Scan"}
    run(scenario())


def test_snapshot_rejects_wrong_parent_and_missing_primary(db):
    async def scenario():
        await db.filings.create_if_absent(make_filing())
        checkpoint = PackageCheckpoint(db.filings, db.artifacts)
        for children, error in (([make_artifact("other.xml")], ValueError),
                                ([make_artifact(), make_artifact()], ValueError),
                                ([make_artifact(company_cik="1")], RepositoryConflict)):
            with pytest.raises(error):
                await checkpoint.persist(make_filing(), children, primary_document_name="Primary.htm", completed_at=NOW)
        assert not db.memory.tables["artifacts"]
        assert (await db.filings.get_checkpoint(ACCESSION)).enumeration_completed_at is None
    run(scenario())


def test_old_work_pagination_stale_index_and_disabled_company(db):
    async def scenario():
        await db.companies.upsert(Company("AAPL", "320193", "Apple", enabled=False))
        children = [make_artifact(f"file-{i}.htm", discovered_at=NOW - timedelta(days=3650-i)) for i in range(5)]
        for child in children:
            await db.artifacts.create_if_absent(child)
        snapshot = deepcopy(list(db.memory.tables["artifacts"].values()))
        db.memory.index_snapshots["PendingArtifactWork"] = snapshot
        # Index still returns the first child as ACQUIRE after durable publication.
        await db.artifacts.mark_stored(children[0].artifact_id, "s3://artifacts/key", NOW, None, None)
        await db.artifacts.mark_published(children[0].artifact_id, NOW)
        first = await db.artifacts.list_pending("ACQUIRE", page_size=1)
        assert not first.items and first.next_token is not None
        result, token = [], first.next_token
        while token is not None:
            page = await db.artifacts.list_pending("ACQUIRE", page_size=1, token=token)
            result.extend(page.items)
            token = page.next_token
        assert result == children[1:]
        with pytest.raises(ValueError):
            await db.artifacts.list_pending("PUBLISH", page_size=1, token=first.next_token)
        with pytest.raises(ValueError):
            await db.artifacts.list_pending("ACQUIRE", page_size=2, token=first.next_token)
        assert all("FilterExpression" not in params for op, params in db.memory.calls if op == "Query")
    run(scenario())


def test_empty_sdk_page_does_not_end_pagination(db):
    async def scenario():
        await db.artifacts.create_if_absent(make_artifact())
        row = next(iter(db.memory.tables["artifacts"].values()))
        db.memory.query_responses.extend([
            {"Items": [], "LastEvaluatedKey": encode({"artifact_id": "unused"})},
            {"Items": [encode({"artifact_id": row["artifact_id"]})]},
        ])
        assert (await db.artifacts.list_pending("ACQUIRE", page_size=2)).items == (make_artifact(),)
        assert len([op for op, _ in db.memory.calls if op == "Query"]) == 2
    run(scenario())


def test_company_enable_disable_and_stale_index(db):
    async def scenario():
        company = Company("AAPL", "320193", "Apple")
        await db.companies.upsert(company)
        assert await db.companies.get("320193") == company
        snapshot = deepcopy(list(db.memory.tables["companies"].values()))
        assert (await db.companies.list_enabled()).items == (company,)
        await db.companies.upsert(replace(company, enabled=False))
        db.memory.index_snapshots["EnabledCompanies"] = snapshot
        assert (await db.companies.list_enabled()).items == ()
        assert "enabled_marker" not in next(iter(db.memory.tables["companies"].values()))
    run(scenario())


def test_calendar_updates_payload_and_paginated_date_range(db):
    async def scenario():
        events = [ExpectedEarningsEvent(company_cik=str(i + 1), ticker=f"T{i}", expected_date=NOW.date(),
                                       synced_at=NOW, provider="fixture", raw_provider_payload={"price": 1.25, "nested": [None, True]}) for i in range(4)]
        await db.calendar.upsert_events(events)
        await db.calendar.upsert_events(events)
        db.memory.query_cap = 1
        assert await db.calendar.get_events(NOW, NOW) == events
        changed = replace(events[0], synced_at=NOW + timedelta(days=1), time_of_day="before_market")
        await db.calendar.upsert_events([changed])
        await db.calendar.upsert_events([events[0]])
        assert (await db.calendar.get_events(NOW, NOW))[0] == changed
        with pytest.raises(RepositoryConflict):
            await db.calendar.upsert_events([replace(changed, time_of_day="after_market")])
        tomorrow = replace(events[1], expected_date=(NOW + timedelta(days=1)).date())
        await db.calendar.upsert_events([tomorrow])
        assert len(await db.calendar.get_events(NOW, NOW + timedelta(days=1))) == 5
        with pytest.raises(ValueError):
            await db.calendar.get_events(NOW, NOW + timedelta(days=366))
        with pytest.raises(ValueError):
            await db.calendar.get_events(NOW, NOW - timedelta(days=1))
    run(scenario())


def test_corrupt_pending_membership_fails_explicitly(db):
    async def scenario():
        await db.artifacts.create_if_absent(make_artifact())
        row = next(iter(db.memory.tables["artifacts"].values()))
        row.pop("pending_work_kind")
        with pytest.raises(RepositoryDataError):
            await db.artifacts.get(make_artifact().artifact_id)
    run(scenario())


def test_filing_failure_before_children_remains_recoverable(db):
    async def scenario():
        filing = make_filing(discovered_at=NOW - timedelta(days=5000))
        await db.filings.create_if_absent(filing)
        await db.filings.record_failure(ACCESSION, "incomplete package", NOW)
        await db.filings.record_failure(ACCESSION, "incomplete package", NOW)
        progress = await db.filings.get_checkpoint(ACCESSION)
        assert progress.retry_count == 1
        assert (await db.filings.list_pending()).items == (filing,)
        assert not db.memory.tables["artifacts"]
    run(scenario())


def test_complete_children_but_failed_parent_commit_recovers(db, monkeypatch):
    async def scenario():
        filing = make_filing()
        await db.filings.create_if_absent(filing)
        checkpoint = PackageCheckpoint(db.filings, db.artifacts)
        original = db.memory.api
        def fail_commit(operation, params):
            if operation == "UpdateItem":
                raise db.client.exceptions.InternalServerError({"Error": {"Code": "InternalServerError"}}, operation)
            return original(operation, params)
        monkeypatch.setattr(db.client, "_make_api_call", fail_commit)
        with pytest.raises(db.client.exceptions.InternalServerError):
            await checkpoint.persist(filing, [make_artifact()], primary_document_name="Primary.htm", completed_at=NOW)
        assert len(db.memory.tables["artifacts"]) == 1
        assert (await db.filings.get_checkpoint(ACCESSION)).enumeration_completed_at is None
        monkeypatch.setattr(db.client, "_make_api_call", original)
        await checkpoint.persist(filing, [make_artifact()], primary_document_name="Primary.htm", completed_at=NOW)
        assert (await db.filings.get_checkpoint(ACCESSION)).enumeration_completed_at == NOW
    run(scenario())


def test_fixture_package_to_durable_children_and_ticker_alias(db):
    async def scenario():
        fixture_root = Path(__file__).parents[1] / "fixtures" / "sec_package"
        original = make_filing()
        await db.filings.create_if_absent(original)
        observed = replace(original, ticker="NEW", discovered_at=NOW + timedelta(days=1))
        package = parse_filing_index(observed, (fixture_root / "index.html").read_bytes(),
                                     json.loads((fixture_root / "directory.json").read_text()))
        await PackageCheckpoint(db.filings, db.artifacts).persist(observed,
            package.artifacts(observed, discovered_at=NOW), primary_document_name=package.primary_document_name, completed_at=NOW)
        page = await db.artifacts.list_for_filing(ACCESSION, page_size=2)
        assert len(page.items) == 2 and page.next_token
        more = await db.artifacts.list_for_filing(ACCESSION, page_size=2, token=page.next_token)
        assert {a.filename for a in (*page.items, *more.items)} == {"Primary.htm", "Slides.PDF", "graphic.jpg", "release.htm"}
        assert all(a.ticker == "AAPL" for a in (*page.items, *more.items))
    run(scenario())


def test_delayed_index_visibility_needs_another_recovery_pass(db):
    async def scenario():
        filing = make_filing()
        await db.filings.create_if_absent(filing)
        db.memory.index_snapshots["PendingFilingEnumeration"] = []
        assert (await db.filings.list_pending()).items == ()
        del db.memory.index_snapshots["PendingFilingEnumeration"]
        assert (await db.filings.list_pending()).items == (filing,)
    run(scenario())


def test_bounded_conditional_contention_propagates(db, monkeypatch):
    async def scenario():
        await db.artifacts.create_if_absent(make_artifact())
        original = db.memory.api
        def contend(operation, params):
            if operation == "UpdateItem":
                raise db.client.exceptions.ConditionalCheckFailedException({"Error": {"Code": "ConditionalCheckFailedException"}}, operation)
            return original(operation, params)
        monkeypatch.setattr(db.client, "_make_api_call", contend)
        with pytest.raises(RepositoryBusy):
            await db.artifacts.record_failure(make_artifact().artifact_id, "oops", NOW)
        assert (await db.artifacts.get(make_artifact().artifact_id)).retry_count == 0
    run(scenario())


@pytest.mark.parametrize("token", ["!bad", "e30=", "W10=", "bm90IGpzb24="])
def test_invalid_cursor_rejected_without_aws_call(db, token):
    with pytest.raises(ValueError):
        run(db.filings.list_pending(token=token))
    assert not db.memory.calls


def test_completed_artifact_failure_does_not_resurrect_pending_work(db):
    async def scenario():
        child = make_artifact()
        await db.artifacts.create_if_absent(child)
        await db.artifacts.mark_stored(child.artifact_id, "s3://artifacts/key", NOW, None, 0)
        await db.artifacts.mark_published(child.artifact_id, NOW)
        await db.artifacts.record_failure(child.artifact_id, "stale failure", NOW + timedelta(days=1))
        assert (await db.artifacts.get(child.artifact_id)).retry_count == 0
        assert not (await db.artifacts.list_pending("ACQUIRE")).items
        assert not (await db.artifacts.list_pending("PUBLISH")).items
    run(scenario())
