"""Atomic local journal for safe launch recovery."""

import json
import os
import tempfile
from pathlib import Path

import portalocker


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
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
