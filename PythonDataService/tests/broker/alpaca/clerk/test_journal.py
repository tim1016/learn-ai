"""Unit tests for the Alpaca Clerk order journal (phase 2, S1).

Append + fsync + reload; the Alpaca-scoped, traversal-safe path.
"""

from __future__ import annotations

from pathlib import Path
from threading import get_ident

import pytest

from app.broker.alpaca.clerk.journal import (
    INBOX_FILENAME,
    JOURNAL_FILENAME,
    ClerkSettings,
    OrderJournal,
)
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg

_ORDER_REF = "manual/inkant/v1:abc123"


def _accepted_order() -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id="broker-order-1",
        client_order_id=_ORDER_REF,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=2.0,
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="accepted",
        submitted_at_ms=1_700_000_000_000,
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_000,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=1_700_000_000_000,
    )


def _entry(kind: ClerkEntryKind = ClerkEntryKind.INTENT_RECORDED) -> OrderJournalEntry:
    # SUBMIT_ACKED always carries the accepted order in the real Clerk (the
    # journal-invariant validator enforces this), so build one for that kind.
    order = _accepted_order() if kind is ClerkEntryKind.SUBMIT_ACKED else None
    return OrderJournalEntry(
        kind=kind,
        account_id="PA-1",
        operator="inkant",
        intent_id="abc123",
        order_ref=_ORDER_REF,
        client_order_id=_ORDER_REF,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        recorded_at_ms=1_700_000_000_000,
        order=order,
    )


def test_append_writes_inbox_and_journal(tmp_path: Path) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)

    journal.append(_entry())

    account_dir = tmp_path / "accounts" / "alpaca" / "PA-1"
    assert (account_dir / INBOX_FILENAME).is_file()
    assert (account_dir / JOURNAL_FILENAME).is_file()
    assert journal.appended == 1


async def test_append_async_offloads_the_durable_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    event_loop_thread = get_ident()
    append_threads: list[int] = []

    def record_append(entry: OrderJournalEntry) -> None:
        assert entry.kind is ClerkEntryKind.INTENT_RECORDED
        append_threads.append(get_ident())

    monkeypatch.setattr(journal, "append", record_append)

    await journal.append_async(_entry())

    assert len(append_threads) == 1
    assert append_threads[0] != event_loop_thread


def test_first_append_fsyncs_the_account_directory_and_new_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    synced: list[Path] = []

    def record_directory_sync(path: Path) -> None:
        synced.append(path)

    monkeypatch.setattr(journal, "_fsync_directory", record_directory_sync)

    journal.append(_entry())

    assert synced == [
        tmp_path / "accounts" / "alpaca" / "PA-1",
        tmp_path / "accounts" / "alpaca",
        tmp_path / "accounts",
        tmp_path,
    ]


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


@pytest.mark.parametrize("account_id", ["../escape", ".", ".."])
def test_unsafe_account_id_is_rejected(tmp_path: Path, account_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe account_id"):
        OrderJournal(account_id=account_id, root=tmp_path)


def test_alpaca_path_is_separate_from_other_brokers(tmp_path: Path) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    # The Alpaca scope segment is always present, so an IBKR journal at the same
    # root would never collide with this account's files.
    assert journal.account_dir == tmp_path / "accounts" / "alpaca" / "PA-1"


def test_default_clerk_dir_uses_the_mounted_artifacts_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_CLERK_DIR", raising=False)

    settings = ClerkSettings(_env_file=None)

    assert settings.dir.name == "alpaca_clerk"
    assert settings.dir.parent.name == "artifacts"
