"""Atomic local heartbeat, not shared research data or a public API."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4


class HealthFile:
    def __init__(self, path):
        self.path = Path(path)
        self.instance = uuid4().hex

    def write(self, snapshot):
        payload = {**snapshot, "instance": self.instance, "pid": os.getpid()}
        temporary = self.path.with_name(self.path.name + "." + self.instance + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, allow_nan=False))
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self):
        if self.path.exists():
            try:
                if json.loads(self.path.read_text()).get("instance") == self.instance:
                    self.path.unlink()
            except (ValueError, OSError):
                pass

    @staticmethod
    def check(path, *, max_age_seconds=90, now=None):
        try:
            value = json.loads(Path(path).read_text())
            at = datetime.fromisoformat(value["heartbeat_at"])
            now = now or datetime.now(timezone.utc)
            age = (now - at).total_seconds()
            return value["live"] is True and 0 <= age < max_age_seconds
        except (ValueError, OSError, KeyError, TypeError):
            return False
