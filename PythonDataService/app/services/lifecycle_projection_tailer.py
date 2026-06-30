"""Tail canonical lifecycle artifacts into the rebuildable Postgres projection.

This module is a service seam, not a router and not a scheduler. It reads
canonical file artifacts, writes projection rows through the replay layer, and
persists a durable file cursor after successful writes.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    ACCOUNT_EVENTS_FILENAME,
    account_artifacts_root,
    read_account_events_tolerant_with_hash,
)
from app.engine.live.intent_wal import IntentWal
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.services.lifecycle_projection_replay import (
    LifecycleProjectionReplayStore,
    batch_from_account_events,
    batch_from_intent_events,
    write_replay_batch,
)

LifecycleProjectionSourceKind = Literal["account_events", "intent_wal"]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class AccountEventsProjectionSource:
    """One account-scoped account_events.jsonl source to project."""

    artifacts_root: Path
    account_id: str

    @property
    def path(self) -> Path:
        return account_artifacts_root(self.artifacts_root, self.account_id) / ACCOUNT_EVENTS_FILENAME


@dataclass(frozen=True)
class IntentWalProjectionSource:
    """One run-scoped intent_events.jsonl source to project."""

    wal_path: Path
    account_id: str
    bot_id: str
    run_id: str
    since_ms: int | None = None
    live_state_last_intent_wal_seq: int | None = None


class LifecycleProjectionSourceCursor(BaseModel):
    """Durable cursor for one canonical source artifact."""

    model_config = ConfigDict(extra="forbid")

    source_kind: LifecycleProjectionSourceKind
    source_artifact: str
    last_file_position: int = Field(default=0, ge=0)
    last_source_seq: int | None = Field(default=None, ge=0)
    source_hash: str | None = Field(default=None, min_length=64, max_length=64)
    updated_at_ms: int = Field(ge=0)


class LifecycleProjectionCursor(BaseModel):
    """Durable cursor file for the lifecycle projection tailer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    sources: dict[str, LifecycleProjectionSourceCursor] = Field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleProjectionTailResult:
    """Summary returned by one tailer pass."""

    rows_written: int
    sources_checked: int
    sources_advanced: int
    cursor: LifecycleProjectionCursor


def default_lifecycle_projection_cursor_path(artifacts_root: Path) -> Path:
    """Return the durable cursor path for one artifacts root."""

    return artifacts_root / "projections" / "lifecycle_projection_cursor.json"


def read_lifecycle_projection_cursor(path: Path) -> LifecycleProjectionCursor:
    """Read a durable projection cursor, returning an empty cursor when absent."""

    if not path.is_file():
        return LifecycleProjectionCursor()
    return LifecycleProjectionCursor.model_validate_json(path.read_text(encoding="utf-8"))


def write_lifecycle_projection_cursor(path: Path, cursor: LifecycleProjectionCursor) -> None:
    """Atomically write a durable projection cursor."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(cursor.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    with _file_lock(path):
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_parent_dir(path)


def merge_lifecycle_projection_cursor_source(
    path: Path,
    *,
    source_key: str,
    source_kind: LifecycleProjectionSourceKind,
    source_artifact: str,
    last_file_position: int,
    last_source_seq: int | None,
    source_hash: str | None,
    updated_at_ms: int,
) -> LifecycleProjectionCursor:
    """Atomically merge one source advancement into the durable cursor."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        cursor = _read_lifecycle_projection_cursor_unlocked(path)
        _advance_cursor(
            cursor,
            source_key=source_key,
            source_kind=source_kind,
            source_artifact=source_artifact,
            last_file_position=last_file_position,
            last_source_seq=last_source_seq,
            source_hash=source_hash,
            updated_at_ms=updated_at_ms,
        )
        data = json.dumps(cursor.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_parent_dir(path)
        return cursor


async def tail_lifecycle_projection_sources(
    *,
    cursor_path: Path,
    account_event_sources: Sequence[AccountEventsProjectionSource] = (),
    intent_wal_sources: Sequence[IntentWalProjectionSource] = (),
    store: LifecycleProjectionReplayStore | None = None,
    inserted_at_ms: int | None = None,
) -> LifecycleProjectionTailResult:
    """Tail canonical artifacts and persist projection rows after the cursor."""

    cursor = read_lifecycle_projection_cursor(cursor_path)
    rows_written = 0
    sources_checked = 0
    sources_advanced = 0
    effective_inserted_at_ms = inserted_at_ms if inserted_at_ms is not None else _now_ms()

    for source in account_event_sources:
        sources_checked += 1
        written, advanced = await _tail_account_events_source(
            source,
            cursor=cursor,
            cursor_path=cursor_path,
            store=store,
            inserted_at_ms=effective_inserted_at_ms,
        )
        rows_written += written
        sources_advanced += int(advanced)

    for source in intent_wal_sources:
        sources_checked += 1
        written, advanced = await _tail_intent_wal_source(
            source,
            cursor=cursor,
            cursor_path=cursor_path,
            store=store,
            inserted_at_ms=effective_inserted_at_ms,
        )
        rows_written += written
        sources_advanced += int(advanced)

    return LifecycleProjectionTailResult(
        rows_written=rows_written,
        sources_checked=sources_checked,
        sources_advanced=sources_advanced,
        cursor=cursor,
    )


async def _tail_account_events_source(
    source: AccountEventsProjectionSource,
    *,
    cursor: LifecycleProjectionCursor,
    cursor_path: Path,
    store: LifecycleProjectionReplayStore | None,
    inserted_at_ms: int,
) -> tuple[int, bool]:
    source_key = _source_key("account_events", source.path)
    source_cursor = cursor.sources.get(source_key)
    last_file_position = source_cursor.last_file_position if source_cursor is not None else 0
    rows, source_hash = read_account_events_tolerant_with_hash(source.artifacts_root, source.account_id)
    if last_file_position > len(rows):
        last_file_position = 0
    new_rows = rows[last_file_position:]
    if not new_rows:
        return 0, False

    batch = batch_from_account_events(
        new_rows,
        account_id=source.account_id,
        source_artifact=str(source.path),
        source_hash=source_hash,
        inserted_at_ms=inserted_at_ms,
        start_file_position=last_file_position + 1,
    )
    written = await write_replay_batch(batch, store=store)
    merged_cursor = merge_lifecycle_projection_cursor_source(
        cursor_path,
        source_key=source_key,
        source_kind="account_events",
        source_artifact=str(source.path),
        last_file_position=len(rows),
        last_source_seq=_last_account_event_source_seq(rows),
        source_hash=source_hash,
        updated_at_ms=inserted_at_ms,
    )
    cursor.sources = merged_cursor.sources
    return written, True


async def _tail_intent_wal_source(
    source: IntentWalProjectionSource,
    *,
    cursor: LifecycleProjectionCursor,
    cursor_path: Path,
    store: LifecycleProjectionReplayStore | None,
    inserted_at_ms: int,
) -> tuple[int, bool]:
    source_key = _source_key("intent_wal", source.wal_path)
    source_cursor = cursor.sources.get(source_key)
    last_source_seq = source_cursor.last_source_seq if source_cursor is not None else 0
    events, source_hash = IntentWal(source.wal_path).read_tail_with_hash()
    if source_cursor is not None and source_cursor.last_file_position > len(events):
        last_source_seq = 0
    new_events = [event for event in events if event.seq > (last_source_seq or 0)]
    if not new_events:
        return 0, False

    batch = batch_from_intent_events(
        new_events,
        bot_id=source.bot_id,
        account_id=source.account_id,
        run_id=source.run_id,
        wal_path=source.wal_path,
        since_ms=source.since_ms,
        live_state_last_intent_wal_seq=source.live_state_last_intent_wal_seq,
        source_hash=source_hash,
        inserted_at_ms=inserted_at_ms,
    )
    written = await write_replay_batch(batch, store=store)
    merged_cursor = merge_lifecycle_projection_cursor_source(
        cursor_path,
        source_key=source_key,
        source_kind="intent_wal",
        source_artifact=str(source.wal_path),
        last_file_position=len(events),
        last_source_seq=events[-1].seq,
        source_hash=source_hash,
        updated_at_ms=inserted_at_ms,
    )
    cursor.sources = merged_cursor.sources
    return written, True


def _advance_cursor(
    cursor: LifecycleProjectionCursor,
    *,
    source_key: str,
    source_kind: LifecycleProjectionSourceKind,
    source_artifact: str,
    last_file_position: int,
    last_source_seq: int | None,
    source_hash: str | None,
    updated_at_ms: int,
) -> None:
    cursor.sources[source_key] = LifecycleProjectionSourceCursor(
        source_kind=source_kind,
        source_artifact=source_artifact,
        last_file_position=last_file_position,
        last_source_seq=last_source_seq,
        source_hash=source_hash,
        updated_at_ms=updated_at_ms,
    )


def _source_key(source_kind: LifecycleProjectionSourceKind, path: Path) -> str:
    return f"{source_kind}:{path.resolve()}"


def _read_lifecycle_projection_cursor_unlocked(path: Path) -> LifecycleProjectionCursor:
    if not path.is_file():
        return LifecycleProjectionCursor()
    return LifecycleProjectionCursor.model_validate_json(path.read_text(encoding="utf-8"))


def _last_account_event_source_seq(rows: list[dict]) -> int | None:
    if not rows:
        return None
    seq = rows[-1].get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1:
        return seq
    return len(rows)


__all__ = [
    "AccountEventsProjectionSource",
    "IntentWalProjectionSource",
    "LifecycleProjectionCursor",
    "LifecycleProjectionSourceCursor",
    "LifecycleProjectionTailResult",
    "default_lifecycle_projection_cursor_path",
    "merge_lifecycle_projection_cursor_source",
    "read_lifecycle_projection_cursor",
    "tail_lifecycle_projection_sources",
    "write_lifecycle_projection_cursor",
]
