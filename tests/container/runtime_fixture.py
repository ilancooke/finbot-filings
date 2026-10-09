"""Real runtime/client/adapters with reusable offline SDK and HTTP boundaries."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import boto3.session

from aws_fakes import MemoryAWS, MemoryMessages, MemoryS3
from finbot_ingestion.domain import Company
from finbot_ingestion.repositories.dynamodb.serialization import record
from finbot_ingestion.runtime.application import RuntimeApplication

RAW = b"\x00SEC\xff\r\noriginal"
ACCESSION = "0000320193-26-000001"


def install():
    root = Path(os.environ.get("FINBOT_TEST_STATE", "/tmp/fixture"))
    root.mkdir(parents=True, exist_ok=True)
    scenario = os.environ["FINBOT_TEST_SCENARIO"]
    lock = threading.RLock()
    clients, memory = {}, {}
    s3, messages = MemoryS3(), MemoryMessages()
    s3.clock = datetime.now(timezone.utc)

    def trace(kind, **values):
        with lock, (root / "trace.jsonl").open("a") as stream:
            stream.write(json.dumps({"kind": kind, **values}) + "\n")

    original_client = boto3.session.Session.client

    def client(session, service_name, *args, **kwargs):
        assert service_name not in clients, "SDK clients must be reused"
        sdk = original_client(session, service_name, *args, **kwargs)
        clients[service_name] = sdk
        if service_name == "dynamodb":
            db = memory["db"] = MemoryAWS(sdk)
            company = record(Company("AAPL", "320193", "Apple fixture"))
            company["enabled_marker"] = "ENABLED"
            db.tables["companies"][(company["cik"],)] = company
            boundary = db.api
        else:
            boundary = s3.api if service_name == "s3" else messages.api

        def api(operation, params):
            result = boundary(operation, params)
            trace("aws", service=service_name, operation=operation)
            return result

        sdk._make_api_call = api
        original_close = sdk.close

        def close():
            trace("client_closed", service=service_name)
            original_close()
            db = memory.get("db")
            if db is not None:
                with db.lock:
                    snapshot = {"tables": list(db.tables["artifacts"].values()),
                        "objects": [body.hex() for body, head in s3.objects.values()],
                        "events": [json.loads(p["Message"]) for op, p in messages.events if op == "Publish"]}
                (root / "result.json").write_text(json.dumps(snapshot, default=str))
                if service_name == "dynamodb":  # Last client closes after runtime cleanup.
                    snapshot["trace"] = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
                    snapshot["health_exists"] = Path("/tmp/finbot-ingestion-health.json").exists()
                    print("FINBOT_FIXTURE_RESULT=" + json.dumps(snapshot, default=str), flush=True)

        sdk.close = close
        return sdk

    boto3.session.Session.client = client

    class Response:
        status_code = 200

        def __init__(self, content, content_type):
            self.content = content
            self.headers = {"Content-Type": content_type, "Content-Length": str(len(content))}

        def iter_content(self, chunk_size):
            yield self.content

        def close(self):
            pass

    class Session:
        def __init__(self):
            self.headers = {}
            self.blocked = False

        def mount(self, *args):
            pass

        def get(self, url, **kwargs):
            trace("sec", url=url)
            if "submissions/CIK" in url:
                payload = {"cik": "320193", "filings": {"recent": {
                    "accessionNumber": [ACCESSION], "form": ["8-K"],
                    "acceptanceDateTime": [s3.clock.isoformat()],
                    "primaryDocument": ["Primary.htm"], "items": ["2.02"]}}}
                return Response(json.dumps(payload).encode(), "application/json")
            fixtures = Path(os.environ.get("FINBOT_TEST_FIXTURES", "/fixtures"))
            if url.endswith("-index.html"):
                return Response((fixtures / "index.html").read_bytes(), "text/html")
            if url.endswith("index.json"):
                return Response((fixtures / "directory.json").read_bytes(), "application/json")
            if scenario == "blocking" and not self.blocked:
                self.blocked = True
                trace("blocked")
                deadline = time.monotonic() + 20
                while not (root / "release").exists():
                    if time.monotonic() >= deadline:
                        raise AssertionError("fixture blocking call was never released")
                    time.sleep(.01)
                trace("released")
            return Response(RAW, "application/octet-stream")

        def close(self):
            trace("sec_closed")

    import requests
    requests.Session = Session

    # Native curl sockets bypass the Python socket guard in sitecustomize.
    from curl_cffi.requests import Session as CurlSession
    def forbidden(*args, **kwargs):
        raise AssertionError("native network is forbidden in lifecycle tests")
    CurlSession.request = forbidden

    if scenario.startswith("yahoo"):
        from yahoo_fakes import raw_page, row
        from finbot_ingestion.calendar.providers.yahoo_client import YahooSession, YahooPageClient
        blocked = False
        def perform(session, method, url, **kwargs):
            nonlocal blocked
            trace("yahoo", operation="calendar" if "visualization" in url else "auth")
            if url.endswith("getcrumb"):
                return SimpleNamespace(status_code=200, content=b"fixture-crumb", text="fixture-crumb", headers={})
            if "visualization" not in url:
                return SimpleNamespace(status_code=404, content=b"", headers={})
            if scenario == "yahoo_blocking" and not blocked:
                blocked = True
                trace("yahoo_blocked")
                deadline = time.monotonic() + 20
                while not (root / "release").exists():
                    if time.monotonic() >= deadline:
                        raise AssertionError("Yahoo fixture blocking call was never released")
                    time.sleep(.01)
                trace("yahoo_released")
            from datetime import date
            body = kwargs["json"]
            day = date.fromisoformat(body["query"]["operands"][2]["operands"][1])
            today = datetime.now(ZoneInfo("America/New_York")).date()
            rows = [row(day)] if day == today and body["offset"] == 0 else []
            payload = raw_page(day, body["size"], body["offset"], rows, int(day == today))
            return SimpleNamespace(status_code=200, content=b"{}", headers={}, json=lambda:payload)
        YahooSession._perform = perform
        original_yahoo_close = YahooPageClient.close
        def close_yahoo(client):
            original_yahoo_close(client)
            trace("yahoo_closed")
        YahooPageClient.close = close_yahoo

    original_stop = RuntimeApplication.request_stop

    def stop(application):
        original_stop(application)
        trace("stop", accepting=application.polls.accepting)

    RuntimeApplication.request_stop = stop
    original_shutdown = RuntimeApplication.shutdown

    async def shutdown(application, *args):
        await original_shutdown(application, *args)
        trace("shutdown", tasks_done=all(t.done() for t in application.tasks.values()))

    RuntimeApplication.shutdown = shutdown
    if scenario in {"failure", "return"}:
        async def scheduler(application):
            await asyncio.sleep(.4)
            trace("required_loop_stopped")
            if scenario == "failure":
                raise RuntimeError("fixture required loop failure")
        RuntimeApplication.scheduler_loop = scheduler
