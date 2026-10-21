"""Image dependencies participate in the normal durable snapshot and recovery path."""

import json

from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.ingestion.work_control import Outcome
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.selection.policy import AcquisitionPolicy
from tests.support.dynamodb import ACCESSION, make_filing
from tests.support.ingestion import run


def image_worker(system, monkeypatch, *, bad=False):
    worker = system.worker()
    filing = make_filing(primary_document_name='Primary.htm')
    worker.discovery.selection = AcquisitionPolicy(filing.filed_at)
    rows = [
        ('Primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'Earnings release'),
        ('page.jpg', 'GRAPHIC', 'Graphic'), ('logo.jpg', 'GRAPHIC', 'Logo'),
        ('deck.htm', 'EX-99.2', 'Earnings presentation'), ('deck.jpg', 'GRAPHIC', 'Graphic')]
    html = '<div class="infoHead">Items</div><div class="info">Item 2.02</div>'
    html += '<table><tr><th>Document</th><th>Type</th><th>Description</th></tr>'
    html += ''.join(f'<tr><td><a href="{name}">{name}</a></td><td>{kind}</td><td>{desc}</td></tr>'
                    for name, kind, desc in rows) + '</table>'
    package = parse_filing_index(filing, html.encode(),
        {'directory': {'item': [{'name': name} for name, _, _ in rows]}})
    monkeypatch.setattr(system.sec, 'get_filing_index', lambda filing: package)
    downloads = []
    bodies = {'Primary.htm': b'<p>See earnings release.</p>',
        'release.htm': b'<img src="logo.jpg" alt="logo"><img src="page.jpg" width="1365" height="769">'
                       b'<font style="font-size:1pt;color:white">Quarter diluted EPS USD 4.60</font>',
        'page.jpg': b'wrong format' if bad else b'\xff\xd8\xffOriginal image bytes'}
    def download(url, *, max_bytes, context=None):
        name = url.rsplit('/', 1)[-1]
        if name.endswith('.jpg'):
            assert max_bytes == 8 * 1024 * 1024
        downloads.append(name)
        assert name in bodies, 'decorative or excluded document was downloaded'
        return DownloadedDocument(bodies[name], 'image/jpeg' if name.endswith('.jpg') else 'text/html', url)
    monkeypatch.setattr(system.sec, 'download_document', download)
    return worker, filing, downloads


def test_images_are_required_members_and_restart_never_redownloads(system, monkeypatch):
    worker, filing, downloads = image_worker(system, monkeypatch)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        result = await worker.enumerate_only(ACCESSION)
        assert len(result.artifact_ids) == 3
        assert not system.messages.events
        assert downloads == ['Primary.htm', 'release.htm']
        # Primary routing, if needed, is reused; each selected body is acquired once.
        assert downloads.count('Primary.htm') == 1
        assert await worker.publish_filing(ACCESSION) == Outcome.DEFERRED
        await RecoveryService(worker).run_pass()
        assert downloads.count('page.jpg') == 1
        assert len(system.s3.objects) == 3
        assert len(system.messages.events) == 1
        event = json.loads(system.messages.events[0][1]['Message'])
        assert {a['filename'] for a in event['artifacts']} == {'Primary.htm', 'release.htm', 'page.jpg'}
        restarted = system.worker()
        restarted.discovery.selection = worker.discovery.selection
        await RecoveryService(restarted).run_pass()
        assert len(downloads) == 3 and len(system.messages.events) == 1
    run(scenario())


def test_invalid_required_image_never_publishes_partial_filing(system, monkeypatch):
    worker, filing, downloads = image_worker(system, monkeypatch, bad=True)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        await worker.process_filing(ACCESSION)
        state = await system.db.artifacts.get_checkpoint(ACCESSION + '/page.jpg')
        assert state.terminal_stage == 'ACQUIRE'
        assert await worker.publish_filing(ACCESSION) == Outcome.DEFERRED
        assert all(operation != 'Publish' for operation, _ in system.messages.events)
    run(scenario())
