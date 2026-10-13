"""Shared offline integration fixtures; networking is blocked at tests/conftest.py."""

from tests.support.container import docker
from tests.support.dynamodb import db
from tests.support.dynamodb_sdk import sdk
from tests.support.ingestion import system
from tests.support.yahoo_sync import no_backoff
