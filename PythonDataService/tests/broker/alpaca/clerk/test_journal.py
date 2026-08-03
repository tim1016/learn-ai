"""Unit tests for the Alpaca Clerk order journal (phase 2, S1).

Append + fsync + reload; the Alpaca-scoped, traversal-safe path.
"""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_append_async_coalesces_projection_requests_after_durable_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import clerk_transaction_projection

    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    projection_started = asyncio.Event()
    release_projection = asyncio.Event()
    calls = 0

    async def project(*, artifacts_root: Path, account_id: str) -> None:
        nonlocal calls
        assert artifacts_root == tmp_path
        assert account_id == "PA-1"
        calls += 1
        projection_started.set()
        await release_projection.wait()

    monkeypatch.setattr(clerk_transaction_projection, "project_alpaca_journal_best_effort", project)

    await journal.append_async(_entry())
    await journal.append_async(_entry())
    await asyncio.wait_for(projection_started.wait(), timeout=1)

    assert calls == 1
    release_projection.set()
    task = journal._projection_task
    assert task is not None
    await task


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


def test_cursor_read_consumes_only_appended_bytes(tmp_path: Path) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    journal.append(_entry(ClerkEntryKind.INTENT_RECORDED))

    cold = journal.read_from(0, file_identity=None)
    warm = journal.read_from(cold.next_offset, file_identity=cold.file_identity)
    journal.append(_entry(ClerkEntryKind.SUBMIT_ACKED))
    appended = journal.read_from(warm.next_offset, file_identity=warm.file_identity)

    assert [entry.kind for entry in cold.entries] == [ClerkEntryKind.INTENT_RECORDED]
    assert warm.entries == ()
    assert warm.bytes_read == 0
    assert [entry.kind for entry in appended.entries] == [ClerkEntryKind.SUBMIT_ACKED]
    assert appended.bytes_read < cold.bytes_read * 2


def test_cursor_read_waits_for_complete_final_record(tmp_path: Path) -> None:
    journal = OrderJournal(account_id="PA-1", root=tmp_path)
    journal.append(_entry())
    cold = journal.read_from(0, file_identity=None)
    path = journal.account_dir / JOURNAL_FILENAME
    partial = _entry(ClerkEntryKind.SUBMIT_ACKED).model_dump_json().encode("utf-8")
    with path.open("ab") as handle:
        handle.write(partial)

    incomplete = journal.read_from(cold.next_offset, file_identity=cold.file_identity)
    with path.open("ab") as handle:
        handle.write(b"\n")
    complete = journal.read_from(incomplete.next_offset, file_identity=incomplete.file_identity)

    assert incomplete.entries == ()
    assert incomplete.next_offset == cold.next_offset
    assert [entry.kind for entry in complete.entries] == [ClerkEntryKind.SUBMIT_ACKED]


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
