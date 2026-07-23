"""Unit tests for the Alpaca Clerk order journal (phase 2, S1).

Append + fsync + reload; the Alpaca-scoped, traversal-safe path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.journal import (
    INBOX_FILENAME,
    JOURNAL_FILENAME,
    OrderJournal,
)
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerOrderLeg


def _entry(kind: ClerkEntryKind = ClerkEntryKind.INTENT_RECORDED) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=kind,
        account_id="PA-1",
        operator="inkant",
        intent_id="abc123",
        order_ref="manual/inkant/v1:abc123",
        client_order_id="manual/inkant/v1:abc123",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        recorded_at_ms=1_700_000_000_000,
    )


def test_append_writes_inbox_and_journal(tmp_path: Path) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)

    journal.append(_entry())

    account_dir = tmp_path / "accounts" / "alpaca" / "PA-1"
    assert (account_dir / INBOX_FILENAME).is_file()
    assert (account_dir / JOURNAL_FILENAME).is_file()
    assert journal.appended == 1


def test_append_then_reload_reconstructs_entries(tmp_path: Path) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    journal.append(_entry(ClerkEntryKind.INTENT_RECORDED))
    journal.append(_entry(ClerkEntryKind.SUBMIT_ACKED))

    reloaded = OrderJournal(account_id="PA-1", root=tmp_path).read_entries()

    assert [e.kind for e in reloaded] == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_ACKED,
    ]
    assert reloaded[0].order_ref == "manual/inkant/v1:abc123"
    assert reloaded[0].leg.symbol == "SPY"


def test_read_entries_on_empty_journal_returns_empty(tmp_path: Path) -> None:
    assert OrderJournal(account_id="PA-1", root=tmp_path).read_entries() == []


def test_unsafe_account_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe account_id"):
        OrderJournal(account_id="../escape", root=tmp_path)


def test_alpaca_path_is_separate_from_other_brokers(tmp_path: Path) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    # The Alpaca scope segment is always present, so an IBKR journal at the same
    # root would never collide with this account's files.
    assert journal.account_dir == tmp_path / "accounts" / "alpaca" / "PA-1"
