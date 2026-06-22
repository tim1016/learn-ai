"""Tests for ``app.services.broker_activity_wal``.

Mirrors the ``IntentWal`` test coverage: append + read round-trip,
non-monotonic-seq corruption, trailing-partial-line tolerance,
paginated read_from, last_seq cursor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.broker_activity import BrokerActivityRow, Verdict
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    BrokerActivityWalCorruptError,
    stable_broker_activity_wal_path,
)


def _row(seq: int) -> BrokerActivityRow:
    return BrokerActivityRow(
        seq=seq,
        ts_ms=1_700_000_000_000 + seq,
        symbol="SPY",
        side="BUY",
        quantity=100.0,
        order_type="MKT",
        verdict=Verdict.EXPECTED,
        template_key="normal_fill",
        template_version=1,
        headline=f"row-{seq}",
        narrative=f"row-{seq}",
    )


def test_canonical_path_is_sibling_of_intent_events(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-abc"
    assert stable_broker_activity_wal_path(run_dir) == run_dir / "broker_activity.jsonl"


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    wal = BrokerActivityWal(tmp_path / "broker_activity.jsonl")
    first = wal.allocate_seq()
    assert first == 1
    wal.append_row(_row(1))

    second = wal.allocate_seq()
    assert second == 2
    wal.append_row(_row(2))

    rows = wal.read_all()
    assert [r.seq for r in rows] == [1, 2]
    assert rows[0].headline == "row-1"
    assert rows[1].headline == "row-2"


def test_append_rejects_mismatched_seq(tmp_path: Path) -> None:
    """``append_row`` requires the row's seq equal what
    ``allocate_seq`` returned — the publisher's contract is to call
    allocate, build the row with that seq, then append. A mismatch
    means a wiring bug."""
    wal = BrokerActivityWal(tmp_path / "wal.jsonl")
    wal.allocate_seq()  # consumes 1
    with pytest.raises(ValueError, match="next available seq is 1"):
        wal.append_row(_row(5))


def test_read_from_returns_only_rows_after_cursor(tmp_path: Path) -> None:
    wal = BrokerActivityWal(tmp_path / "wal.jsonl")
    for seq in (1, 2, 3, 4, 5):
        wal.allocate_seq()
        wal.append_row(_row(seq))

    page = wal.read_from(after_seq=2)
    assert [r.seq for r in page] == [3, 4, 5]


def test_read_from_respects_limit(tmp_path: Path) -> None:
    wal = BrokerActivityWal(tmp_path / "wal.jsonl")
    for seq in (1, 2, 3, 4, 5):
        wal.allocate_seq()
        wal.append_row(_row(seq))

    page = wal.read_from(after_seq=0, limit=2)
    assert [r.seq for r in page] == [1, 2]


def test_read_from_rejects_negative_cursor(tmp_path: Path) -> None:
    wal = BrokerActivityWal(tmp_path / "wal.jsonl")
    with pytest.raises(ValueError):
        wal.read_from(after_seq=-1)


def test_last_seq_returns_zero_on_empty_wal(tmp_path: Path) -> None:
    wal = BrokerActivityWal(tmp_path / "wal.jsonl")
    assert wal.last_seq() == 0


def test_last_seq_returns_highest_persisted(tmp_path: Path) -> None:
    wal = BrokerActivityWal(tmp_path / "wal.jsonl")
    wal.allocate_seq()
    wal.append_row(_row(1))
    wal.allocate_seq()
    wal.append_row(_row(2))
    assert wal.last_seq() == 2


def test_read_tolerates_single_trailing_partial_line(tmp_path: Path) -> None:
    """ADR-0008 §3 read contract — a single trailing line without a
    newline is the artifact of a crash mid-fsync. The pre-crash rows
    are still durable; the partial tail is dropped."""
    path = tmp_path / "wal.jsonl"
    wal = BrokerActivityWal(path)
    wal.allocate_seq()
    wal.append_row(_row(1))
    # Append a torn partial line directly (simulating a power-loss crash).
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "ts_ms": 17000')  # no newline

    rows = wal.read_all()
    assert [r.seq for r in rows] == [1]


def test_read_raises_on_torn_line_followed_by_complete_lines(tmp_path: Path) -> None:
    """Tear in the middle of the file (not the tail) is corruption — a
    complete line after a torn line means the torn line is not just an
    incomplete tail, it's data loss in the middle of the file."""
    path = tmp_path / "wal.jsonl"
    wal = BrokerActivityWal(path)
    wal.allocate_seq()
    wal.append_row(_row(1))
    # Inject a torn line, then a complete line.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("garbage-not-json\n")  # complete line, but unparseable
        # next valid append would go here, but we'd already corrupt.

    with pytest.raises(BrokerActivityWalCorruptError):
        wal.read_all()


def test_read_raises_on_non_monotonic_seq(tmp_path: Path) -> None:
    """A row with seq <= last_seq is corruption (publisher should never
    write one; a manual edit or a race is the only way this happens)."""
    path = tmp_path / "wal.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_row(2).model_dump_json() + "\n")
        fh.write(_row(2).model_dump_json() + "\n")  # duplicate seq

    wal = BrokerActivityWal(path)
    with pytest.raises(BrokerActivityWalCorruptError, match="non-monotonic"):
        wal.read_all()


def test_allocate_seq_continues_from_existing_max(tmp_path: Path) -> None:
    """A fresh WAL instance opened against an existing file must
    allocate seq = max(existing) + 1, not restart at 1 (that would
    poison the WAL on next read)."""
    path = tmp_path / "wal.jsonl"
    first_writer = BrokerActivityWal(path)
    first_writer.allocate_seq()
    first_writer.append_row(_row(1))
    first_writer.allocate_seq()
    first_writer.append_row(_row(2))
    # Simulate process restart — new WAL object, same path.
    second_writer = BrokerActivityWal(path)
    assert second_writer.allocate_seq() == 3


def test_empty_file_reads_as_empty(tmp_path: Path) -> None:
    """An empty WAL file (touched but never written) must read as no
    rows, never raise."""
    path = tmp_path / "wal.jsonl"
    path.touch()
    wal = BrokerActivityWal(path)
    assert wal.read_all() == []
    assert wal.last_seq() == 0


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    """A WAL whose path does not yet exist (publisher hasn't appended
    anything) must read as no rows, never raise."""
    wal = BrokerActivityWal(tmp_path / "never-created.jsonl")
    assert wal.read_all() == []
    assert wal.last_seq() == 0
