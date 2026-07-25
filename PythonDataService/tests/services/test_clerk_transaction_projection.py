from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_owner import (
    MANUAL_OPERATOR_RUN_ID,
    MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
    MANUAL_ORDER_INTENT_KIND,
    AccountOwnerSubmitIntent,
)
from app.schemas.clerk_transaction_projection import ClerkTransactionRow, ClerkTransactionSummaryRow
from app.services import clerk_transaction_projection
from app.services.clerk_transaction_projection import (
    ClerkJournalCursor,
    ClerkTransactionBatch,
    fold_lifecycle_state,
    project_account_journal_best_effort,
    rebuild_account_transaction_projection,
    read_appended_clerk_journal,
    recover_account_transaction_feed,
    tail_account_journal,
    transaction_detail,
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
        self.semantic_event_keys: set[tuple[str, str]] = set()
        self.fail = False
        self.feed = ("stale", "Stale", "No current projection-feed receipt.", None, None, False)

    async def read_cursor(self, account_id: str, journal_path: str) -> ClerkJournalCursor | None:
        assert account_id == ACCOUNT
        return self.cursor

    async def persist_batch(self, batch: ClerkTransactionBatch, *, updated_at_ms: int) -> int:
        if self.fail:
            raise RuntimeError("projection database unavailable")
        self.persisted.append(batch)
        existing = {row.transaction_id for row in self.rows}
        for row in batch.transactions:
            if row.transaction_id not in existing:
                self.rows.append(row)
                existing.add(row.transaction_id)
        self.semantic_event_keys.update((event.broker, event.callback_identity) for event in batch.events)
        self.cursor = ClerkJournalCursor(batch.account_id, batch.journal_path, batch.next_byte_offset, batch.next_journal_seq, updated_at_ms)
        return len(batch.transactions)

    async def history_page(self, *, account_id: str, limit: int, after: tuple[int, int, str] | None):
        assert account_id == ACCOUNT
        rows = sorted(self.rows, key=lambda row: (row.recorded_at_ms, row.journal_seq, row.transaction_id), reverse=True)
        if after is not None:
            rows = [row for row in rows if (row.recorded_at_ms, row.journal_seq, row.transaction_id) < after]
        high_water = self.cursor.last_journal_seq if self.cursor else None
        lag = max((high_water or 0) - max((row.journal_seq for row in self.rows), default=0), 0) if high_water else None
        return [
            ClerkTransactionSummaryRow.model_validate(
                {**row.model_dump(exclude={"receipt", "events"}), "event_count": len(row.events)}
            )
            for row in rows[:limit]
        ], high_water, lag

    async def transaction_detail(self, *, account_id: str, transaction_id: str) -> ClerkTransactionRow | None:
        assert account_id == ACCOUNT
        return next((row for row in self.rows if row.transaction_id == transaction_id), None)

    async def feed_status(self, account_id: str) -> tuple[str, str, str, int | None, int | None, bool]:
        assert account_id == ACCOUNT
        return self.feed

    async def set_feed_status(
        self, account_id: str, state: str, headline: str, detail: str, *, updated_at_ms: int,
        high_water_journal_seq: int | None = None, lag_records: int | None = None,
        lag_is_lower_bound: bool = False,
    ) -> None:
        assert account_id == ACCOUNT
        self.feed = state, headline, detail, high_water_journal_seq, lag_records, lag_is_lower_bound

    async def reset_projection(
        self, *, account_id: str, journal_path: str, broker: str, updated_at_ms: int
    ) -> None:
        assert account_id == ACCOUNT
        self.rows = [row for row in self.rows if row.broker != broker]
        self.cursor = None
        self.feed = ("rebuilding", "Rebuild in progress", "Projection rebuild is replaying canonical Clerk acknowledgement evidence.", 0, 0, False)


class _CountingStore(_Store):
    """Production-shaped test store: the database retains rows, Python does not."""

    def __init__(self) -> None:
        super().__init__()
        self.projected_transactions = 0
        self.batch_count = 0
        self.max_batch_records = 0

    async def persist_batch(self, batch: ClerkTransactionBatch, *, updated_at_ms: int) -> int:
        self.projected_transactions += len(batch.transactions)
        self.batch_count += 1
        self.max_batch_records = max(self.max_batch_records, batch.source_record_count)
        self.cursor = ClerkJournalCursor(
            batch.account_id, batch.journal_path, batch.next_byte_offset, batch.next_journal_seq, updated_at_ms
        )
        return len(batch.transactions)


def _callback(
    seq: int,
    *,
    event_type: str,
    status: str | None,
    fee: float | None = None,
    callback_identity: str | None = None,
) -> AccountClerkJournalEntry:
    ack = _manual_ack(1)
    event = IbkrOrderEvent(
        account_id=ACCOUNT, order_id=ack.order_id or 0, perm_id=ack.perm_id,
        event_type=event_type, status=status, order_ref=ack.intent.order_ref if ack.intent else None,
        fill_quantity=1.0 if event_type == "fill" else None, remaining=0.0 if status == "Filled" else 1.0,
        fee=fee, ts_ms=1_700_000_000_000 + seq,
    )
    return AccountClerkJournalEntry(
        seq=seq, entry_kind="broker_event", recorded_at_ms=event.ts_ms, intent=ack.intent,
        broker_event=event.model_dump(mode="json"), event_account_id=ACCOUNT,
        broker_callback_idempotency_key=callback_identity or f"callback-{seq}-{event_type}-{status}",
    )


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


def test_read_appended_clerk_journal_respects_byte_bound_and_resumes_from_cursor(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    _append(path, _manual_ack(1))
    _append(path, _manual_ack(2))
    first_record_size = path.read_bytes().index(b"\n") + 1

    first = read_appended_clerk_journal(
        account_id=ACCOUNT,
        journal_path=path,
        cursor=None,
        max_read_bytes=first_record_size,
    )
    assert first is not None
    assert first.next_journal_seq == 1
    second = read_appended_clerk_journal(
        account_id=ACCOUNT,
        journal_path=path,
        cursor=ClerkJournalCursor(ACCOUNT, str(path), first.next_byte_offset, first.next_journal_seq, 1),
        max_read_bytes=first_record_size,
    )
    assert second is not None
    assert second.next_journal_seq == 2


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


def test_read_appended_clerk_journal_is_bounded_by_record_budget(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    for seq in range(1, 503):
        _append(path, _manual_ack(seq, 1_700_000_000_000 + seq))

    first = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)

    assert first is not None
    assert first.source_record_count == clerk_transaction_projection.MAX_JOURNAL_TAIL_RECORDS
    assert first.has_more_complete_records is True
    assert len(first.transactions) == clerk_transaction_projection.MAX_JOURNAL_TAIL_RECORDS
    second = read_appended_clerk_journal(
        account_id=ACCOUNT,
        journal_path=path,
        cursor=ClerkJournalCursor(ACCOUNT, str(path), first.next_byte_offset, first.next_journal_seq, 1),
    )
    assert second is not None
    assert second.source_record_count == 2
    assert second.has_more_complete_records is False


@pytest.mark.asyncio
async def test_corrupt_complete_record_keeps_canonical_ack_and_marks_projection_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    path.write_bytes(path.read_bytes() + b'{"seq":not-json}\n')
    store = _Store()

    with pytest.raises(clerk_transaction_projection.ClerkTransactionProjectionCorrupt):
        await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=2)

    assert path.read_bytes().endswith(b'{"seq":not-json}\n')
    assert store.cursor is None
    assert store.feed[0] == "corrupt"


def test_read_appended_clerk_journal_rejects_cursor_beyond_canonical_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    _append(path, _manual_ack(1))

    with pytest.raises(clerk_transaction_projection.ClerkTransactionProjectionCorrupt, match="truncated"):
        read_appended_clerk_journal(
            account_id=ACCOUNT,
            journal_path=path,
            cursor=ClerkJournalCursor(ACCOUNT, str(path), path.stat().st_size + 1, 1, 1),
        )


@pytest.mark.asyncio
async def test_duplicate_callback_identity_is_projected_once_while_cursor_advances(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    first = _callback(2, event_type="fill", status="Filled")
    duplicate = first.model_copy(update={"seq": 3, "recorded_at_ms": first.recorded_at_ms + 1})
    _append(path, first)
    _append(path, duplicate)
    store = _Store()

    await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=1)

    assert store.cursor is not None and store.cursor.last_journal_seq == 3
    assert store.semantic_event_keys == {
        ("ibkr", "journal:1:broker_acked"),
        ("ibkr", first.broker_callback_idempotency_key or ""),
    }


@pytest.mark.asyncio
async def test_rebuild_replaces_only_projection_with_canonical_equivalent_receipts(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    _append(path, _callback(2, event_type="fill", status="Filled", fee=1.25))
    store = _Store()
    await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=1)
    before = [(row.transaction_id, row.receipt) for row in store.rows]

    assert await rebuild_account_transaction_projection(
        artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=2
    ) == 1

    assert [(row.transaction_id, row.receipt) for row in store.rows] == before
    assert store.cursor is not None and store.cursor.last_journal_seq == 2
    assert store.feed[0] == "live"


@pytest.mark.asyncio
async def test_recovery_stops_after_its_bounded_window_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    store = _Store()
    monkeypatch.setattr(clerk_transaction_projection, "MAX_RECOVERY_BATCHES", 0)

    assert await recover_account_transaction_feed(
        artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=10
    ) == 0

    assert store.feed[0] == "reconnecting"
    assert store.feed[4] == 1
    assert store.feed[5] is True


@pytest.mark.slow
@pytest.mark.asyncio
async def test_100k_canonical_journal_replay_stays_within_incremental_budget(tmp_path: Path) -> None:
    """Repeatable scale fixture; run explicitly with ``pytest -m slow`` in CI/ops."""

    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    for seq in range(1, 100_001):
        _append(path, _manual_ack(seq, 1_700_000_000_000 + seq))
    store = _CountingStore()
    tracemalloc.start()
    started = time.perf_counter()

    projected = 0
    while store.feed[0] != "live":
        projected += await recover_account_transaction_feed(
            artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=100
        )

    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert projected == 100_000
    assert store.projected_transactions == 100_000
    assert store.batch_count == 200
    assert store.max_batch_records == clerk_transaction_projection.MAX_JOURNAL_TAIL_RECORDS
    assert store.cursor is not None and store.cursor.last_journal_seq == 100_000
    assert store.feed[0] == "live"
    assert peak_bytes <= 16 * 1024 * 1024
    assert time.perf_counter() - started < 60


@pytest.mark.asyncio
async def test_best_effort_projection_failure_cannot_block_durable_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _fail(**kwargs) -> int:
        raise RuntimeError("projection database unavailable")

    monkeypatch.setattr(clerk_transaction_projection.settings, "CLERK_TRANSACTION_PROJECTION_ENABLED", True)
    monkeypatch.setattr(clerk_transaction_projection, "tail_account_journal", _fail)

    await project_account_journal_best_effort(artifacts_root=tmp_path, account_id=ACCOUNT)

    assert "Clerk transaction projection failed after durable acknowledgement" in caplog.text


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
    assert "receipt" not in first.rows[0].model_dump()
    assert first.next_cursor is not None and first.next_cursor.startswith("ctxhp1.")
    second = await transaction_history(account_id=ACCOUNT, limit=2, cursor=first.next_cursor, store=store)
    assert [row.journal_seq for row in second.rows] == [1]


@pytest.mark.asyncio
async def test_selected_transaction_receipt_is_read_separately_from_the_grid_page(tmp_path: Path) -> None:
    store = _Store()
    path = tmp_path / "journal.jsonl"
    _append(path, _manual_ack(1))
    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)
    assert batch is not None
    await store.persist_batch(batch, updated_at_ms=1)

    page = await transaction_history(account_id=ACCOUNT, limit=25, cursor=None, store=store)
    detail = await transaction_detail(
        account_id=ACCOUNT, transaction_id=page.rows[0].transaction_id, store=store
    )

    assert detail is not None
    assert detail.receipt["intent"]["order_ref"] == "learn-ai/manual/v1:manual-1"


@pytest.mark.asyncio
async def test_lifecycle_callbacks_fold_terminal_state_without_fabricating_commission(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    _append(path, _callback(2, event_type="fill", status="Submitted"))
    _append(path, _callback(3, event_type="cancel", status="Cancelled"))
    _append(path, _callback(4, event_type="fill", status="Filled", fee=1.25))
    store = _Store()

    await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store)
    events = store.persisted[0].events
    assert [event.lifecycle_state for event in events] == ["submitted", "partially_filled", "cancelled", "filled"]
    assert events[1].commission_status == "unknown"
    assert events[-1].fee == 1.25
    state = "submitted"
    for event in events[1:]:
        state = fold_lifecycle_state(state, event.lifecycle_state)
    assert state == "cancelled"


def test_duplicate_callback_identity_produces_the_same_projection_event_id(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    _append(path, _manual_ack(1))
    _append(path, _callback(2, event_type="fill", status="Filled", callback_identity="ibkr-exec-1"))
    _append(path, _callback(3, event_type="fill", status="Filled", callback_identity="ibkr-exec-1"))

    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)

    assert batch is not None
    assert [event.callback_identity for event in batch.events[1:]] == ["ibkr-exec-1", "ibkr-exec-1"]
    assert batch.events[1].event_id == batch.events[2].event_id


@pytest.mark.asyncio
async def test_recovery_publishes_only_after_commit_and_marks_live(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    store = _Store()
    published: list[int] = []

    async def publish(batch: ClerkTransactionBatch) -> None:
        assert store.cursor is not None
        published.append(batch.next_journal_seq)

    count = await recover_account_transaction_feed(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, publish=publish, updated_at_ms=100)
    assert count == 1
    assert published == [1]
    assert store.feed[0] == "live"


@pytest.mark.asyncio
async def test_recovery_stops_at_its_byte_bounded_batch_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    first_record_size = path.read_bytes().index(b"\n") + 1
    _append(path, _manual_ack(2))
    monkeypatch.setattr(clerk_transaction_projection, "MAX_PROJECTION_READ_BYTES", first_record_size)
    monkeypatch.setattr(clerk_transaction_projection, "MAX_RECOVERY_BATCHES", 1)
    store = _Store()

    projected = await recover_account_transaction_feed(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        store=store,
        updated_at_ms=100,
    )

    assert projected == 1
    assert store.feed[0] == "reconnecting"
