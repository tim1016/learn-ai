"""Read-contract tests for historical intent-WAL evidence.

A single trailing partial line is tolerated, any other malformation poisons
the read, and a complete unacknowledged ``PENDING_INTENT`` is returned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)
from tests._helpers.legacy_ibkr_artifacts import (
    write_historical_intent_wal,
)

NS = build_bot_order_namespace("foo")


def _pending(seq: int) -> IntentEvent:
    iid = mint_intent_id()
    return IntentEvent(
        seq=seq,
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
    )


def test_intent_event_ts_ms_bounded_to_int64() -> None:
    """ts_ms is serialized into the WAL, so it must honor the repo's int64-ms
    boundary contract rather than accept an arbitrary-width int (CodeRabbit
    review on the #448 re-merge)."""
    from pydantic import ValidationError

    iid = mint_intent_id()
    common = {
        "seq": 1,
        "event_type": IntentEventType.PENDING_INTENT,
        "intent_id": iid,
        "bot_order_namespace": NS,
        "order_ref": build_order_ref(NS, iid),
    }

    # In-range is accepted.
    assert IntentEvent(**common, ts_ms=1_780_000_000_000).ts_ms == 1_780_000_000_000
    # Above int64 max and negative are rejected at the boundary.
    with pytest.raises(ValidationError):
        IntentEvent(**common, ts_ms=9_223_372_036_854_775_808)
    with pytest.raises(ValidationError):
        IntentEvent(**common, ts_ms=-1)


def test_round_trip_read_tail(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    written = [_pending(seq) for seq in range(1, 4)]
    write_historical_intent_wal(path, written)
    wal = IntentWal(path)
    read = wal.read_tail()
    assert [e.seq for e in read] == [1, 2, 3]
    assert [e.intent_id for e in read] == [e.intent_id for e in written]


def test_complete_pending_intent_is_returned_not_dropped(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    ev = _pending(1)
    write_historical_intent_wal(path, [ev])
    wal = IntentWal(path)
    tail = wal.read_tail()
    assert len(tail) == 1
    assert tail[0].event_type is IntentEventType.PENDING_INTENT
    assert tail[0].intent_id == ev.intent_id


def test_trailing_partial_line_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    write_historical_intent_wal(path, [_pending(1), _pending(2)])
    wal = IntentWal(path)
    # Simulate a crash mid-write: a third line with no terminating newline.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "event_type": "PENDING_INTENT", "partial')
    tail = wal.read_tail()
    assert [e.seq for e in tail] == [1, 2]  # partial trailing line dropped


def test_malformed_complete_line_poisons(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    # A complete (newline-terminated) malformed line is corruption, not a tail.
    path.write_text("not json at all\n", encoding="utf-8")
    wal = IntentWal(path)
    with pytest.raises(IntentWalCorruptError):
        wal.read_tail()


def test_torn_line_with_complete_lines_after_poisons(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    good = _pending(1)
    write_historical_intent_wal(path, [good])
    wal = IntentWal(path)
    # Prepend a malformed complete line before the good one.
    body = path.read_text(encoding="utf-8")
    path.write_text("{bad json}\n" + body, encoding="utf-8")
    assert good.seq == 1
    with pytest.raises(IntentWalCorruptError):
        wal.read_tail()


def test_non_monotonic_seq_poisons(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    iid = mint_intent_id()
    dup = IntentEvent(
        seq=1,
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
    )
    line = dup.model_dump_json() + "\n"
    path.write_text(line + line, encoding="utf-8")  # seq 1 then seq 1 again
    wal = IntentWal(path)
    with pytest.raises(IntentWalCorruptError):
        wal.read_tail()
