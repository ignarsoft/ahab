"""Atomic local journal for safe launch recovery."""

import json
import os
import tempfile
import time
from pathlib import Path

import portalocker


class Store:
    def __init__(self, directory, *, create=True):
        self.directory = Path(directory).expanduser()
        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def lock(self):
        return portalocker.Lock(str(self.directory / "hunter.lock"), timeout=0)

    def load(self):
        path = self.directory / "state.json"
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("state.json must be an object; review manually")
        return value

    def status(self, now=None):
        """Return a redacted journal snapshot, not a process liveness check."""
        now = time.time() if now is None else now
        state = self.load()
        result = {"phase": "not_started", "attempts": 0}
        if not state:
            return result
        # Never expose the plan, launch payload, SSH key or retry token.
        result.update(
            phase="ready",
            attempts=state.get("attempts", 0),
            updated_at=state.get("updated_at"),
            instance_id=state.get("instance_id"),
            notification_delivered=bool(state.get("notified")),
        )
        if state.get("instance_id"):
            result["phase"] = "accepted"
            return result
        remaining = max(0, state.get("next_at", 0) - now)
        result["next_check_in_seconds"] = remaining
        if remaining:
            result["phase"] = "waiting"
        pending = state.get("pending")
        if pending:
            age = max(0, now - pending["created_at"])
            result.update(
                phase="uncertain",
                pending_age_seconds=age,
                manual_reconciliation_required=age >= 23 * 3600,
            )
        return result

    def save(self, value, filename="state.json"):
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / filename)
            if os.name == "posix":
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)
