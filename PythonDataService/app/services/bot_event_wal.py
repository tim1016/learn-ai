"""Append-only WAL for raw bot events (ADR 0024 / PRD #928).

The raw WAL is the enforcement/observation capture for the narrated bot
event stream. Authored projections can be rebuilt from it; this module only
persists and reads ``BotEventRaw`` records.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.live_state_sidecar import _fsync_parent_dir
from app.schemas.bot_events import BotEventRaw


class BotEventWalCorruptError(RuntimeError):
    """Raised on malformed raw bot-event WAL content."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"bot-event WAL at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


def run_bot_event_wal_path(run_dir: Path) -> Path:
    """Canonical run-scoped raw bot-event WAL path."""

    return run_dir / "bot_events.jsonl"


class BotEventRawWal:
    """Append-only JSONL store for ``BotEventRaw`` records."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def allocate_seq(self) -> int:
        return self._allocate_seq()

    def append_event(self, event: BotEventRaw) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._truncate_tolerated_tail()
        expected_seq = self._allocate_seq()
        if event.seq != expected_seq:
            raise ValueError(
                f"bot-event WAL append got event.seq={event.seq} but next "
                f"available seq is {expected_seq}"
            )
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
            self._next_seq = event.seq + 1
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(self._path)

    def read_all(self) -> list[BotEventRaw]:
        if not self._path.exists():
            return []
        raw = self._path.read_bytes()
        if not raw:
            return []
        ends_with_newline = raw.endswith(b"\n")
        byte_lines = raw.split(b"\n")
        if byte_lines and byte_lines[-1] == b"":
            byte_lines.pop()

        events: list[BotEventRaw] = []
        last_seq = 0
        n = len(byte_lines)
        for idx, bline in enumerate(byte_lines):
            if idx == n - 1 and not ends_with_newline:
                break
            try:
                event = BotEventRaw.model_validate_json(bline)
            except (ValidationError, ValueError) as exc:
                raise BotEventWalCorruptError(
                    self._path, f"unparseable line {idx + 1}: {exc}"
                ) from exc
            if event.seq <= last_seq:
                raise BotEventWalCorruptError(
                    self._path,
                    f"non-monotonic seq at line {idx + 1}: {event.seq} after {last_seq}",
                )
            last_seq = event.seq
            events.append(event)
        return events

    def _allocate_seq(self) -> int:
        existing = self.read_all()
        disk_next_seq = (existing[-1].seq + 1) if existing else 1
        if self._next_seq is None or self._next_seq < disk_next_seq:
            self._next_seq = disk_next_seq
        return self._next_seq

    def _truncate_tolerated_tail(self) -> None:
        if not self._path.exists():
            return
        raw = self._path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        last_newline = raw.rfind(b"\n")
        truncate_at = last_newline + 1 if last_newline >= 0 else 0
        with open(self._path, "rb+") as fh:
            fh.truncate(truncate_at)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(self._path)


__all__ = [
    "BotEventRawWal",
    "BotEventWalCorruptError",
    "run_bot_event_wal_path",
]
