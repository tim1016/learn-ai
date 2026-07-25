from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_owner import (
    MANUAL_OPERATOR_RUN_ID,
    MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
    MANUAL_ORDER_INTENT_KIND,
    AccountOwnerSubmitIntent,
)
from app.schemas.clerk_transaction_projection import ClerkTransactionRow
from app.services.clerk_transaction_projection import (
    ClerkJournalCursor,
    ClerkTransactionBatch,
    read_appended_clerk_journal,
    tail_account_journal,
    transaction_history,
)

ACCOUNT = "DU1219"


def _manual_ack(seq: int, recorded_at_ms: int = 1_700_000_000_000) -> AccountClerkJournalEntry:
    intent_id = f"manual-{seq}"
    namespace = "learn-ai/manual/v1"
    return AccountClerkJournalEntry(
        seq=seq,
        entry_kind="broker_acked",
        recorded_at_ms=recorded_at_ms,
        intent=AccountOwnerSubmitIntent(
            trace_id=f"trace-{seq}", account_id=ACCOUNT,
            strategy_instance_id=MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
            run_id=MANUAL_OPERATOR_RUN_ID, bot_order_namespace=namespace,
            intent_id=intent_id, order_ref=f"{namespace}:{intent_id}",
            intent_kind=MANUAL_ORDER_INTENT_KIND, order_spec={"symbol": "SPY"},
            owner_generation=1, created_at_ms=recorded_at_ms,
        ), order_id=9000 + seq, perm_id=8000 + seq,
    )


def _append(path: Path, entry: AccountClerkJournalEntry, *, newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(entry.model_dump_json(exclude_none=True).encode("utf-8"))
        if newline:
            handle.write(b"\n")


class _Store:
    def __init__(self) -> None:
        self.cursor: ClerkJournalCursor | None = None
        self.rows: list[ClerkTransactionRow] = []
        self.persisted: list[ClerkTransactionBatch] = []
        self.fail = False

    async def read_cursor(self, account_id: str, journal_path: str) -> ClerkJournalCursor | None:
        assert account_id == ACCOUNT
        return self.cursor

    async def persist_batch(self, batch: ClerkTransactionBatch, *, updated_at_ms: int) -> int:
        if self.fail:
            raise RuntimeError("projection database unavailable")
        self.persisted.append(batch)
        self.rows.extend(batch.transactions)
        self.cursor = ClerkJournalCursor(batch.account_id, batch.journal_path, batch.next_byte_offset, batch.next_journal_seq, updated_at_ms)
        return len(batch.transactions)

    async def history_page(self, *, account_id: str, limit: int, after: tuple[int, int, str] | None):
        assert account_id == ACCOUNT
        rows = sorted(self.rows, key=lambda row: (row.recorded_at_ms, row.journal_seq, row.transaction_id), reverse=True)
        if after is not None:
            rows = [row for row in rows if (row.recorded_at_ms, row.journal_seq, row.transaction_id) < after]
        high_water = self.cursor.last_journal_seq if self.cursor else None
        lag = max((high_water or 0) - max((row.journal_seq for row in self.rows), default=0), 0) if high_water else None
        return rows[:limit], high_water, lag


@pytest.mark.asyncio
async def test_tail_projects_manual_ack_and_resumes_only_appended_bytes(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    store = _Store()

    assert await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=10) == 1
    assert store.rows[0].transaction_kind == "manual_ibkr_acknowledgement"
    first_offset = store.cursor.last_byte_offset if store.cursor else -1

    _append(path, _manual_ack(2, 1_700_000_000_100))
    assert await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=11) == 1
    assert store.persisted[-1].next_byte_offset > first_offset
    assert [row.journal_seq for row in store.rows] == [1, 2]


def test_read_appended_clerk_journal_leaves_torn_record_for_later(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    _append(path, _manual_ack(1), newline=False)
    assert read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None) is None

    with path.open("ab") as handle:
        handle.write(b"\n")
    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)
    assert batch is not None
    assert batch.next_journal_seq == 1
    assert len(batch.transactions) == 1


def test_read_appended_clerk_journal_rejects_malformed_complete_record(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b'{"seq":not-json}\n')

    with pytest.raises(ValueError, match="malformed complete"):
        read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)


@pytest.mark.asyncio
async def test_projection_failure_does_not_change_durable_acknowledgement_or_cursor(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    durable_journal = path.read_bytes()
    store = _Store()
    store.fail = True

    with pytest.raises(RuntimeError, match="database unavailable"):
        await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store)

    assert path.read_bytes() == durable_journal
    assert store.cursor is None
    assert store.rows == []


@pytest.mark.asyncio
async def test_history_uses_opaque_keyset_cursor_and_bounded_page(tmp_path: Path) -> None:
    store = _Store()
    for seq in range(1, 4):
        entry = _manual_ack(seq, 1_700_000_000_000 + seq)
        path = tmp_path / f"journal-{seq}.jsonl"
        _append(path, entry)
        batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)
        assert batch is not None
        await store.persist_batch(batch, updated_at_ms=seq)

    first = await transaction_history(account_id=ACCOUNT, limit=2, cursor=None, store=store)
    assert len(first.rows) == 2
    assert first.next_cursor is not None and first.next_cursor.startswith("ctxhp1.")
    second = await transaction_history(account_id=ACCOUNT, limit=2, cursor=first.next_cursor, store=store)
    assert [row.journal_seq for row in second.rows] == [1]
