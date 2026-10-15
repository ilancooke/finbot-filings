"""FilingReady 1.0 wire contract, immutable manifest identity and validation."""

from dataclasses import replace
from datetime import timedelta, timezone
import json

import pytest

from finbot_ingestion.domain.filing_ready import FilingReady, ReadyArtifact
from tests.support.dynamodb import ACCESSION, NOW, make_artifact, make_filing


def ready():
    artifacts = [replace(make_artifact(name), stored_at=NOW, size_bytes=3,
        s3_uri='s3://artifacts/AAPL/' + name) for name in ('Primary.htm', 'release.htm')]
    return FilingReady.from_artifacts(make_filing(), artifacts,
        primary_document_name='Primary.htm', company_name='Apple', ready_at=NOW)


def test_manifest_identity_and_canonical_wire_encoding():
    event = ready()
    payload = event.to_dict()
    assert payload['event_type'] == 'filing.ready' and payload['schema_version'] == '1.0'
    assert payload['filed_at'] == '2026-10-08T19:59:50.000000Z'
    assert payload['ready_at'] == '2026-10-08T20:00:00.000000Z'
    assert payload['artifacts'][0] == dict(artifact_id=ACCESSION + '/Primary.htm', filename='Primary.htm',
        s3_uri='s3://artifacts/AAPL/Primary.htm', size_bytes=3)
    assert payload['snapshot_id'] == '29329bd911d01c5b947a0fed704928e364944b0d8f3590e8a5f87fc88779ba1e'
    assert replace(event, artifacts=tuple(reversed(event.artifacts))).to_json() == event.to_json()
    assert replace(event, ready_at=NOW + timedelta(days=1)).snapshot_id == event.snapshot_id
    assert replace(event, filed_at=event.filed_at.astimezone(timezone(timedelta(hours=2)))).snapshot_id == event.snapshot_id
    assert replace(event, company_name='Apple Inc.').snapshot_id != event.snapshot_id
    assert replace(event, artifacts=(*event.artifacts, ReadyArtifact(artifact_id=ACCESSION + '/résumé.htm',
        filename='résumé.htm', s3_uri='s3://artifacts/AAPL/r%C3%A9sum%C3%A9.htm', size_bytes=1))).snapshot_id != event.snapshot_id
    assert FilingReady.from_json(event.to_json()) == event


@pytest.mark.parametrize('change', [
    {'artifacts': ()}, {'artifacts': (ready().artifacts[0],) * 2},
    {'primary_artifact_id': ACCESSION + '/missing.htm'}, {'company_name': ''},
    {'ready_at': NOW.replace(tzinfo=None)}, {'filing_id': '0000320193-26-000002'},
])
def test_invalid_filing_notifications_are_rejected(change):
    with pytest.raises(ValueError):
        replace(ready(), **change)


@pytest.mark.parametrize('field,value', [('snapshot_id', '0' * 64), ('event_type', 'artifact.ready'),
                                        ('schema_version', '2.0')])
def test_saved_notification_requires_known_schema_and_matching_hash(field, value):
    payload = ready().to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        FilingReady.from_json(json.dumps(payload, sort_keys=True, separators=(',', ':')))


def test_ready_requires_every_artifact_stored_and_matching_the_filing():
    with pytest.raises(ValueError, match='every artifact stored'):
        FilingReady.from_artifacts(make_filing(), [make_artifact()],
            primary_document_name='Primary.htm', company_name='Apple', ready_at=NOW)
    stored = replace(make_artifact(), stored_at=NOW, s3_uri='s3://artifacts/Primary.htm', size_bytes=3)
    with pytest.raises(ValueError, match='company_cik'):
        FilingReady.from_artifacts(make_filing(company_cik='1234'), [stored],
            primary_document_name='Primary.htm', company_name='Apple', ready_at=NOW)
