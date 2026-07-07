"""Append-only WAL for raw bot events (ADR 0024 / PRD #928).

The raw WAL is the enforcement/observation capture for the narrated bot
event stream. Authored projections can be rebuilt from it; this module only
persists and reads ``BotEventRaw`` records.
"""

from __future__ import annotations

from pathlib import Path

from app.schemas.bot_events import BotEventRaw
from app.services.jsonl_wal import JsonlWal


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
        self._wal = JsonlWal(
            path,
            record_model=BotEventRaw,
            corrupt_error=BotEventWalCorruptError,
            seq_of=lambda event: event.seq,
            label="bot-event",
        )

    @property
    def path(self) -> Path:
        return self._wal.path

    def allocate_seq(self) -> int:
        return self._wal.allocate_seq()

    def append_event(self, event: BotEventRaw) -> None:
        try:
            self._wal.append(event)
        except ValueError as exc:
            raise ValueError(
                f"bot-event WAL append got event.seq={event.seq} but next "
                f"available seq is {self._wal.allocate_seq()}"
            ) from exc

    def read_all(self) -> list[BotEventRaw]:
        return self._wal.read_all()


__all__ = [
    "BotEventRawWal",
    "BotEventWalCorruptError",
    "run_bot_event_wal_path",
]
