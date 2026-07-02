"""Module C (IntentWal) unit tests — ADR-0008 §3 / PRD #446 test plan C.

fsync-before-placeOrder via a spy; monotonic per-run seq; append/read-tail
round-trip; the read contract (single trailing partial line tolerated; any
other malformation poisons; a complete un-acked PENDING_INTENT is returned,
not dropped).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)

NS = build_bot_order_namespace("foo")


def _pending(wal: IntentWal) -> IntentEvent:
    iid = mint_intent_id()
    return wal.append(
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
    )


def test_append_assigns_monotonic_seq(tmp_path: Path) -> None:
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    assert [_pending(wal).seq for _ in range(3)] == [1, 2, 3]


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


def test_pending_intent_fsynced_before_place_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    # Patching the os module's attribute covers every importer (intent_wal's
    # file fsync AND the reused parent-dir fsync), since both do `import os`.
    monkeypatch.setattr(os, "fsync", spy_fsync)

    wal = IntentWal(tmp_path / "intent_events.jsonl")
    _pending(wal)  # append() must fsync before it returns

    def place_order() -> None:
        calls.append("placeOrder")

    place_order()

    assert "fsync" in calls
    assert calls[0] == "fsync"  # nothing happens before the durability barrier
    assert calls.index("placeOrder") > calls.index("fsync")


def test_round_trip_read_tail(tmp_path: Path) -> None:
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    written = [_pending(wal) for _ in range(3)]
    read = wal.read_tail()
    assert [e.seq for e in read] == [1, 2, 3]
    assert [e.intent_id for e in read] == [e.intent_id for e in written]


def test_complete_pending_intent_is_returned_not_dropped(tmp_path: Path) -> None:
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    ev = _pending(wal)
    tail = wal.read_tail()
    assert len(tail) == 1
    assert tail[0].event_type is IntentEventType.PENDING_INTENT
    assert tail[0].intent_id == ev.intent_id


def test_trailing_partial_line_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    wal = IntentWal(path)
    _pending(wal)
    _pending(wal)
    # Simulate a crash mid-write: a third line with no terminating newline.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "event_type": "PENDING_INTENT", "partial')
    tail = wal.read_tail()
    assert [e.seq for e in tail] == [1, 2]  # partial trailing line dropped


def test_append_truncates_tolerated_trailing_partial_before_write(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    wal = IntentWal(path)
    _pending(wal)
    _pending(wal)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "event_type": "PENDING_INTENT", "partial')

    reopened = IntentWal(path)
    appended = _pending(reopened)

    assert appended.seq == 3
    assert [event.seq for event in reopened.read_tail()] == [1, 2, 3]
    assert "partial" not in path.read_text(encoding="utf-8")


def test_malformed_complete_line_poisons(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    # A complete (newline-terminated) malformed line is corruption, not a tail.
    path.write_text("not json at all\n", encoding="utf-8")
    wal = IntentWal(path)
    with pytest.raises(IntentWalCorruptError):
        wal.read_tail()


def test_torn_line_with_complete_lines_after_poisons(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    wal = IntentWal(path)
    good = _pending(wal)
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


def test_seq_consumed_even_if_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "intent_events.jsonl"
    wal = IntentWal(path)
    _pending(wal)  # seq 1, clean (real fsync, pre-patch)

    state = {"calls": 0}
    real_fsync = os.fsync

    def flaky_fsync(fd: int) -> None:
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("simulated fsync failure after the bytes were written")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky_fsync)
    with pytest.raises(OSError):
        _pending(wal)  # seq 2 bytes written, then fsync raises

    # The seq must have advanced despite the failure: the next append is seq 3,
    # not a duplicate seq 2 (which would poison read_tail).
    assert _pending(wal).seq == 3
    assert [e.seq for e in wal.read_tail()] == [1, 2, 3]


def test_seq_continues_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "intent_events.jsonl"
    first = IntentWal(path)
    _pending(first)
    _pending(first)
    # A fresh writer (new process) on the same file resumes the seq.
    second = IntentWal(path)
    assert _pending(second).seq == 3
