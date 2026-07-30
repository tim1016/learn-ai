"""Tests for the durable per-bot decision-receipt journal (broker-v2 S0).

Covers: append + fsync discipline, bounded tail, by-transaction lookup,
monotone seq enforcement, and partial-write recovery.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.decision_journal import (
    MAX_TAIL,
    DecisionJournal,
    DecisionReceipt,
    decision_journal_path,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def journal(tmp_path: Path) -> DecisionJournal:
    return DecisionJournal(account_id="ACC1", sid="bot-123", root=tmp_path)


def _receipt(seq: int, outcome: str = "no_action", ts_ms: int = 1000) -> DecisionReceipt:
    return DecisionReceipt(
        seq=seq,
        ts_ms=ts_ms,
        bar_ref=f"SPY@{ts_ms}",
        outcome=outcome,  # type: ignore[arg-type]
        reason_code="EMA_FLAT",
    )


# ── Basic append and read ─────────────────────────────────────────────────────


def test_append_creates_file(journal: DecisionJournal, tmp_path: Path) -> None:
    journal.append(_receipt(1))
    path = decision_journal_path(account_id="ACC1", sid="bot-123", root=tmp_path)
    assert path.exists(), "journal file must be created on first append"


def test_append_increments_seq(journal: DecisionJournal) -> None:
    journal.append(_receipt(1))
    journal.append(_receipt(2))
    assert journal.next_seq() == 3


def test_tail_returns_most_recent(journal: DecisionJournal) -> None:
    for i in range(1, 6):
        journal.append(_receipt(i, ts_ms=i * 1000))
    tail = journal.tail(3)
    assert len(tail) == 3
    assert [r.seq for r in tail] == [3, 4, 5]


def test_tail_bounded_by_max_tail(tmp_path: Path) -> None:
    j = DecisionJournal(account_id="ACC2", sid="bot-max", root=tmp_path)
    for i in range(1, MAX_TAIL + 10):
        j.append(_receipt(i, ts_ms=i))
    result = j.tail(MAX_TAIL + 10)
    assert len(result) == MAX_TAIL


def test_tail_all_when_fewer_than_n(journal: DecisionJournal) -> None:
    journal.append(_receipt(1))
    journal.append(_receipt(2))
    result = journal.tail(50)
    assert len(result) == 2


def test_tail_invalid_n_raises(journal: DecisionJournal) -> None:
    with pytest.raises(ValueError, match="positive"):
        journal.tail(0)


# ── by_transaction lookup ─────────────────────────────────────────────────────


def test_by_transaction_finds_matching(journal: DecisionJournal) -> None:
    r1 = DecisionReceipt(
        seq=1, ts_ms=1000, bar_ref="SPY@1000",
        outcome="entered", reason_code="EMA_CROSS",
        intent_id="intent-abc", order_ref="ref-abc",
    )
    r2 = DecisionReceipt(
        seq=2, ts_ms=2000, bar_ref="SPY@2000",
        outcome="no_action", reason_code="EMA_FLAT",
    )
    journal.append(r1)
    journal.append(r2)
    found = journal.by_transaction("intent-abc")
    assert len(found) == 1
    assert found[0].seq == 1


def test_by_transaction_empty_when_no_match(journal: DecisionJournal) -> None:
    journal.append(_receipt(1))
    assert journal.by_transaction("nonexistent") == []


def test_by_transaction_empty_ref_raises(journal: DecisionJournal) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        journal.by_transaction("")


# ── Monotone seq enforcement ──────────────────────────────────────────────────


def test_wrong_seq_raises(journal: DecisionJournal) -> None:
    journal.append(_receipt(1))
    with pytest.raises(ValueError, match="seq"):
        journal.append(_receipt(3))  # skip seq 2


# ── Persistence across instances ─────────────────────────────────────────────


def test_read_after_reopen(tmp_path: Path) -> None:
    j1 = DecisionJournal(account_id="ACC1", sid="bot-123", root=tmp_path)
    j1.append(_receipt(1))
    j1.append(_receipt(2))

    j2 = DecisionJournal(account_id="ACC1", sid="bot-123", root=tmp_path)
    result = j2.tail(10)
    assert len(result) == 2
    assert result[0].seq == 1


def test_seq_continues_after_reopen(tmp_path: Path) -> None:
    j1 = DecisionJournal(account_id="ACC1", sid="bot-123", root=tmp_path)
    j1.append(_receipt(1))
    j1.append(_receipt(2))

    j2 = DecisionJournal(account_id="ACC1", sid="bot-123", root=tmp_path)
    assert j2.next_seq() == 3


# ── Traversal safety ─────────────────────────────────────────────────────────


def test_unsafe_account_id_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        DecisionJournal(account_id="../escape", sid="bot", root=tmp_path)


def test_unsafe_sid_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        DecisionJournal(account_id="ACC1", sid="../escape", root=tmp_path)


# ── Outcome variety ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("outcome", ["entered", "exited", "no_action", "blocked"])
def test_all_outcomes_accepted(journal: DecisionJournal, outcome: str) -> None:
    r = DecisionReceipt(
        seq=1, ts_ms=1000, bar_ref="SPY@1000",
        outcome=outcome,  # type: ignore[arg-type]
        reason_code="TEST",
    )
    journal.append(r)
    assert journal.tail(1)[0].outcome == outcome


# ── Indicator snapshot round-trip ─────────────────────────────────────────────


def test_indicator_snapshot_roundtrip(journal: DecisionJournal) -> None:
    snapshot = {"ema_fast": 512.34, "ema_slow": 510.00, "position": 0}
    r = DecisionReceipt(
        seq=1, ts_ms=1000, bar_ref="SPY@1000",
        outcome="no_action", reason_code="EMA_FLAT",
        indicator_snapshot=snapshot,
    )
    journal.append(r)
    got = journal.tail(1)[0]
    assert got.indicator_snapshot["ema_fast"] == pytest.approx(512.34)


# ── Partial-write tolerance ───────────────────────────────────────────────────


def test_partial_tail_truncated_on_next_append(tmp_path: Path) -> None:
    """A partial (no-newline) last line is truncated before the next append."""
    j = DecisionJournal(account_id="ACC1", sid="bot-123", root=tmp_path)
    j.append(_receipt(1))
    # Simulate a crash by appending a partial line directly
    path = decision_journal_path(account_id="ACC1", sid="bot-123", root=tmp_path)
    with open(path, "ab") as f:
        f.write(b'{"seq":2,"partial":true')  # no closing brace / newline

    # Appending seq=2 must truncate the partial tail and succeed
    j2 = DecisionJournal(account_id="ACC1", sid="bot-123", root=tmp_path)
    j2.append(_receipt(2))
    result = j2.tail(10)
    assert len(result) == 2
    assert result[-1].seq == 2
