"""Tests for bot-event stream backfill/live cursor delivery."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.bot_events import BotEventIdentity, BotEventRaw, BotEventRawType, SourceAuthority
from app.services.bot_event_stream_service import (
    BotEventStreamService,
    BotEventStreamUnavailableError,
)
from app.services.bot_event_wal import BotEventRawWal, run_bot_event_wal_path

pytestmark = pytest.mark.asyncio

RUN_ID = "run-stream"
SID = "bot-stream"


def _raw(seq: int, event_type: BotEventRawType = BotEventRawType.SIGNAL_FIRED) -> BotEventRaw:
    return BotEventRaw(
        seq=seq,
        ts_ms=1_700_000_000_000 + seq,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        event_type=event_type,
        source_authority=SourceAuthority.ENGINE_LOOP,
        identity=BotEventIdentity(evaluation_id=f"eval-{seq}"),
    )


def _append(run_dir: Path, *events: BotEventRaw) -> None:
    wal = BotEventRawWal(run_bot_event_wal_path(run_dir))
    for event in events:
        wal.append_event(event)


async def test_stream_run_replays_backlog_then_newly_appended_rows(tmp_path: Path) -> None:
    service = BotEventStreamService()
    _append(tmp_path, _raw(1), _raw(2))

    stream = service.stream_run(run_dir=tmp_path, since_seq=0, poll_interval_s=0)
    try:
        first = await anext(stream)
        second = await anext(stream)
        _append(tmp_path, _raw(3))
        third = await anext(stream)
    finally:
        await stream.aclose()

    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert service._channels == {}


async def test_stream_run_honors_since_seq_cursor(tmp_path: Path) -> None:
    service = BotEventStreamService()
    _append(tmp_path, _raw(1), _raw(2))

    stream = service.stream_run(run_dir=tmp_path, since_seq=1, poll_interval_s=0)
    try:
        row = await anext(stream)
    finally:
        await stream.aclose()

    assert row.seq == 2
    assert service._channels == {}


async def test_stream_run_raises_unavailable_for_corrupt_history(tmp_path: Path) -> None:
    service = BotEventStreamService()
    run_bot_event_wal_path(tmp_path).write_text("{not-json}\n", encoding="utf-8")

    stream = service.stream_run(run_dir=tmp_path, since_seq=0, poll_interval_s=0)
    try:
        with pytest.raises(BotEventStreamUnavailableError):
            await anext(stream)
    finally:
        await stream.aclose()
