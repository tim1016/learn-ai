"""Atomic, bounded receipts shared by scheduler jobs and cheap health reads.

The lock protects read/modify/replace across processes. Postgres job ownership,
not this file lock, authorizes provider work. Lost sessions are never pruned.
"""
from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.data_lake.atomic import _fsync_path
from app.option_archive.backup import RestoreReceipt
from app.option_archive.planner import SKIPPED_TONIGHT, SessionOutcome
from app.schemas.option_archive import ArchiveJobName, ArchivePhase, TimestampMs
from app.utils.session_anchors import et_date_at_ms


class _Receipt(BaseModel):
    model_config = ConfigDict(validate_assignment=True)


class SessionReceipt(_Receipt):
    outcome: SessionOutcome | None = None
    reason: str | None = None
    recorded_ms: TimestampMs | None = None
    started_ms: TimestampMs | None = None
    ended_ms: TimestampMs | None = None
    listed: int | None = None
    answered: int | None = None
    empty: int | None = None
    with_bars: int | None = None

    @property
    def complete(self) -> bool:
        return self.outcome is SessionOutcome.COMPLETE

    def skipped_on(self, day: date) -> bool:
        """Whether an attempt that ended on ``day`` keeps this session out of that day's remaining passes."""
        return self.outcome in SKIPPED_TONIGHT and self.ended_ms is not None and et_date_at_ms(self.ended_ms) == day


class JobReceipt(_Receipt):
    started_ms: TimestampMs | None = None
    ended_ms: TimestampMs | None = None
    failure: str | None = None
    retry_after_ms: TimestampMs | None = None
    completed_day_ms: TimestampMs | None = None
    last_success_ms: TimestampMs | None = None
    #: The capture pass only.
    phase: ArchivePhase | None = None
    generation_id: str | None = None
    manifest_sha256: str | None = None
    #: The backup job only.
    captured_through_ms: TimestampMs | None = None
    size_bytes: int | None = None
    free_bytes: int | None = None
    unverified_generations: list[str] = Field(default_factory=list)
    #: The restore check only.
    counts: dict[str, int] | None = None
    receipt: RestoreReceipt | None = None


class SnapshotReceipt(_Receipt):
    complete: bool
    reason: str | None = None
    field_gaps: dict[str, int] = Field(default_factory=dict)
    started_ms: TimestampMs
    ended_ms: TimestampMs


class ProviderBlock(_Receipt):
    session_ms: TimestampMs
    reason: str
    detail: str
    recorded_ms: TimestampMs


class ArchiveStateDoc(_Receipt):
    version: Literal[1]
    baseline_ms: TimestampMs | None = None
    snapshot_activation_ms: TimestampMs | None = None
    sessions: dict[str, dict[int, SessionReceipt]] = Field(default_factory=dict)
    # Keyed by ``ArchiveJobName``; pydantic 2.5 cannot read a ``Literal`` dict key from JSON.
    jobs: dict[str, JobReceipt] = Field(default_factory=dict)
    snapshots: dict[str, dict[int, SnapshotReceipt]] = Field(default_factory=dict)
    provider_blocks: dict[str, ProviderBlock] = Field(default_factory=dict)

    def job(self, name: ArchiveJobName) -> JobReceipt:
        """The job's receipt; an empty one when it never ran. Reading never adds it."""
        return self.jobs.get(name) or JobReceipt()

    def session(self, symbol: str, session_ms: int) -> SessionReceipt:
        """The session's receipt, added when missing."""
        return self.sessions.setdefault(symbol, {}).setdefault(session_ms, SessionReceipt())


class ArchiveState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> ArchiveStateDoc:
        if not self.path.exists():
            return ArchiveStateDoc(version=1)
        return ArchiveStateDoc.model_validate_json(self.path.read_bytes())

    def update(self, mutate: Callable[[ArchiveStateDoc], None]) -> ArchiveStateDoc:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self.read()
            mutate(state)
            payload = json.dumps(state.model_dump(mode="json", exclude_defaults=True),
                                 sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            temp = self.path.with_suffix(".tmp")
            with temp.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            _fsync_path(self.path.parent)
            return state
