"""Offline infrastructure tests; jsii IPC is local, AWS lookups are forbidden."""
import os
from pathlib import Path
import sys
import socket

os.environ.setdefault("JSII_RUNTIME_PACKAGE_CACHE", "/private/tmp/finbot-phase8-jsii" if sys.platform == "darwin" else "/tmp/finbot-phase8-jsii")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
sys.path.insert(0, str(Path(__file__).parents[1]))

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("infrastructure tests cannot contact AWS")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
