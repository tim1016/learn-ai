from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    SourceAuthority,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.services.bot_event_wal import (
    BotEventRawWal,
    BotEventWalCorruptError,
    run_bot_event_wal_path,
)


def _raw(seq: int) -> BotEventRaw:
    return BotEventRaw(
        seq=seq,
        ts_ms=1_700_000_000_000 + seq,
        strategy_instance_id="sid-bot-event-wal",
        run_id="run-bot-event-wal",
        event_type=BotEventRawType.ORDER_REJECTED,
        source_authority=SourceAuthority.BROKER_SESSION,
        identity=BotEventIdentity(order_ref="learn-ai/sid/v1:intent-1", req_id=42),
        terminal_error=TerminalError(
            code=TerminalErrorCode.ORDER_REJECTED,
            source=TerminalErrorSource.IBKR,
            gate_id="broker.place_order",
            message="IBKR order rejected",
            external_code=201,
            external_message="Order rejected",
        ),
    )


def test_run_bot_event_wal_path_is_run_scoped(tmp_path: Path) -> None:
    assert run_bot_event_wal_path(tmp_path / "run-1") == tmp_path / "run-1" / "bot_events.jsonl"


def test_bot_event_wal_allocates_appends_and_reads(tmp_path: Path) -> None:
    wal = BotEventRawWal(run_bot_event_wal_path(tmp_path))

    event = _raw(wal.allocate_seq())
    wal.append_event(event)

    assert wal.read_all() == [event]
    assert wal.allocate_seq() == 2


def test_bot_event_wal_rejects_wrong_seq(tmp_path: Path) -> None:
    wal = BotEventRawWal(run_bot_event_wal_path(tmp_path))

    with pytest.raises(ValueError, match="next available seq"):
        wal.append_event(_raw(3))


def test_bot_event_wal_detects_non_monotonic_rows(tmp_path: Path) -> None:
    path = run_bot_event_wal_path(tmp_path)
    path.write_text(_raw(1).model_dump_json() + "\n" + _raw(1).model_dump_json() + "\n")

    with pytest.raises(BotEventWalCorruptError, match="non-monotonic seq"):
        BotEventRawWal(path).read_all()


def test_bot_event_wal_truncates_tolerated_tail_before_append(tmp_path: Path) -> None:
    path = run_bot_event_wal_path(tmp_path)
    path.write_text(_raw(1).model_dump_json() + "\n" + '{"seq":')
    wal = BotEventRawWal(path)

    event = _raw(wal.allocate_seq())
    wal.append_event(event)

    assert wal.read_all() == [_raw(1), event]
