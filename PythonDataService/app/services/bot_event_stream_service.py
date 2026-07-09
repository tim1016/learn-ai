"""Read/project bot-event WAL history for HTTP consumers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from app.schemas.bot_events import BotEventRow
from app.services.bot_event_projection import project_bot_event_rows
from app.services.bot_event_wal import (
    BotEventRawWal,
    BotEventWalCorruptError,
    run_bot_event_wal_path,
)


class BotEventStreamUnavailableError(RuntimeError):
    """Raised when persisted bot-event history cannot be projected."""


@dataclass(frozen=True)
class BotEventStreamPage:
    rows: list[BotEventRow]
    next_seq: int | None


class BotEventStreamService:
    """Backfill authored bot-event rows from a run-scoped raw WAL."""

    def backfill_run(
        self,
        *,
        run_dir: Path,
        after_seq: int,
        limit: int,
    ) -> BotEventStreamPage:
        try:
            raw_events = BotEventRawWal(
                run_bot_event_wal_path(run_dir), trusted_root=run_dir
            ).read_all()
            rows = sorted(
                project_bot_event_rows(raw_events),
                key=lambda row: row.seq,
            )
        except (BotEventWalCorruptError, ValueError) as exc:
            raise BotEventStreamUnavailableError(
                "bot-event stream history cannot be projected"
            ) from exc

        remaining = [row for row in rows if row.seq > after_seq]
        page_rows = remaining[:limit]
        next_seq = page_rows[-1].seq if len(remaining) > len(page_rows) else None
        return BotEventStreamPage(rows=page_rows, next_seq=next_seq)

    async def stream_run(
        self,
        *,
        run_dir: Path,
        since_seq: int,
        poll_interval_s: float,
        page_limit: int = 500,
    ) -> AsyncIterator[BotEventRow]:
        """Yield authored rows after ``since_seq``, then poll for live rows."""

        last_seq = since_seq
        while True:
            page = self.backfill_run(
                run_dir=run_dir,
                after_seq=last_seq,
                limit=page_limit,
            )
            for row in page.rows:
                if row.seq <= last_seq:
                    continue
                yield row
                last_seq = row.seq
            if page.next_seq is not None:
                continue
            await asyncio.sleep(poll_interval_s)


_SERVICE = BotEventStreamService()


def get_bot_event_stream_service() -> BotEventStreamService:
    return _SERVICE


__all__ = [
    "BotEventStreamPage",
    "BotEventStreamService",
    "BotEventStreamUnavailableError",
    "get_bot_event_stream_service",
]
