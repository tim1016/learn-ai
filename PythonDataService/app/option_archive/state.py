"""Atomic, bounded receipts shared by scheduler jobs and cheap health reads.

The lock protects read/modify/replace across processes. Postgres job ownership,
not this file lock, authorizes provider work. Lost sessions are never pruned.
"""
from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.data_lake.atomic import _fsync_path


class ArchiveState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "sessions": {}, "jobs": {}, "snapshots": {}}
        state = json.loads(self.path.read_bytes())
        if state.get("version") != 1:
            raise ValueError("unsupported option archive state version")
        return state

    def update(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self.read()
            mutate(state)
            payload = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            temp = self.path.with_suffix(".tmp")
            with temp.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            _fsync_path(self.path.parent)
            return state
