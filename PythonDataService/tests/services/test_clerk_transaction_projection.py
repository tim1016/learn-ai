from __future__ import annotations

import asyncio
import time
import tracemalloc
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.journal import JOURNAL_FILENAME
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerOrder
from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_owner import (
    MANUAL_OPERATOR_RUN_ID,
    MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
    MANUAL_ORDER_INTENT_KIND,
    AccountOwnerSubmitIntent,
)
from app.schemas.clerk_transaction_projection import ClerkTransactionRow, ClerkTransactionSummaryRow
from app.services import (
    clerk_transaction_projection,
    clerk_transaction_projection_schema,
    clerk_transaction_projection_store,
)
from app.services.clerk_transaction_projection import (
    ClerkJournalCursor,
    ClerkTransactionBatch,
    ClerkTransactionProjectionUnavailable,
    fold_lifecycle_state,
    project_account_journal_best_effort,
    read_appended_clerk_journal,
    rebuild_account_transaction_projection,
    recover_account_transaction_feed,
    recover_alpaca_account_transaction_feed,
    tail_account_journal,
    tail_alpaca_account_journal,
    transaction_detail,
    transaction_history,
)

ACCOUNT = "DU1219"


def test_projection_schema_expectations_include_journal_generation() -> None:
    for expectation in (
        clerk_transaction_projection_schema.CLERK_TRANSACTIONS,
        clerk_transaction_projection_schema.CLERK_TRANSACTION_EVENTS,
        clerk_transaction_projection_schema.CLERK_TRANSACTION_PROJECTION_CURSORS,
    ):
        assert "journal_generation" in {column.name for column in expectation.columns}


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


def _ack_for_intent_kind(
    seq: int,
    intent_kind: str,
    recorded_at_ms: int = 1_700_000_000_000,
) -> AccountClerkJournalEntry:
    intent_id = f"{intent_kind.lower()}-{seq}"
    namespace = f"learn-ai/{intent_kind.lower()}/v1"
    return AccountClerkJournalEntry(
        seq=seq,
        entry_kind="broker_acked",
        recorded_at_ms=recorded_at_ms,
        intent=AccountOwnerSubmitIntent(
            trace_id=f"trace-{seq}", account_id=ACCOUNT,
            strategy_instance_id=f"bot-{intent_kind.lower()}", run_id=f"run-{intent_kind.lower()}",
            bot_order_namespace=namespace, intent_id=intent_id, order_ref=f"{namespace}:{intent_id}",
            intent_kind=intent_kind, order_spec={"symbol": "SPY"}, owner_generation=1,
            created_at_ms=recorded_at_ms,
        ),
        order_id=9000 + seq,
        perm_id=8000 + seq,
    )


def _intent_stage(
    seq: int,
    intent_kind: str,
    entry_kind: str,
    recorded_at_ms: int = 1_700_000_000_000,
) -> AccountClerkJournalEntry:
    return _ack_for_intent_kind(seq, intent_kind, recorded_at_ms).model_copy(
        update={"entry_kind": entry_kind, "order_id": None, "perm_id": None}
    )


def _append(path: Path, entry: AccountClerkJournalEntry, *, newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(entry.model_dump_json(exclude_none=True).encode("utf-8"))
        if newline:
            handle.write(b"\n")


def _alpaca_ack() -> OrderJournalEntry:
    order_ref = "manual/inkant/v1:alpaca-1"
    return OrderJournalEntry(
        kind=ClerkEntryKind.SUBMIT_ACKED,
        account_id=ACCOUNT,
        operator="inkant",
        intent_id="alpaca-1",
        order_ref=order_ref,
        client_order_id=order_ref,
        recorded_at_ms=1_700_000_000_000,
        order=BrokerOrder(
            broker="alpaca",
            order_id="alpaca-order-1",
            client_order_id=order_ref,
            symbol="SPY",
            asset_class="us_equity",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=1.0,
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
            observed_at_ms=1_700_000_000_000,
        ),
    )


def _append_alpaca(path: Path, entry: OrderJournalEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry.model_dump_json() + "\n", encoding="utf-8")


class _Store:
    def __init__(self) -> None:
        self.cursor: ClerkJournalCursor | None = None
        self.rows: list[ClerkTransactionRow] = []
        self.persisted: list[ClerkTransactionBatch] = []
        self.semantic_event_keys: set[tuple[str, str]] = set()
        self.fail = False
        self.reset_calls = 0
        self.feed = ("stale", "Stale", "No current projection-feed receipt.", None, None, False)

    async def read_cursor(self, account_id: str, journal_path: str) -> ClerkJournalCursor | None:
        assert account_id == ACCOUNT
        return self.cursor

    async def persist_batch(
        self, batch: ClerkTransactionBatch, *, updated_at_ms: int, allow_rebuilding: bool = False
    ) -> int:
        if self.fail:
            raise RuntimeError("projection database unavailable")
        if self.feed[0] == "rebuilding" and not allow_rebuilding:
            raise ClerkTransactionProjectionUnavailable("transaction projection rebuild is in progress")
        self.persisted.append(batch)
        existing = {row.transaction_id for row in self.rows}
        for row in batch.transactions:
            if row.transaction_id not in existing:
                self.rows.append(row)
                existing.add(row.transaction_id)
        self.semantic_event_keys.update((event.broker, event.callback_identity) for event in batch.events)
        self.cursor = ClerkJournalCursor(
            batch.account_id,
            batch.journal_path,
            batch.next_byte_offset,
            batch.next_journal_seq,
            updated_at_ms,
            batch.journal_generation,
        )
        return len({row.transaction_id for row in batch.transactions})

    async def history_page(
        self,
        *,
        account_id: str,
        limit: int,
        after: tuple[int, int, str] | None,
        origin=None,
        lifecycle_state=None,
        strategy_instance_id=None,
        run_id=None,
    ):
        assert account_id == ACCOUNT
        rows = sorted(self.rows, key=lambda row: (row.recorded_at_ms, row.journal_seq, row.transaction_id), reverse=True)
        rows = [
            row
            for row in rows
            if (origin is None or row.transaction_origin == origin)
            and (lifecycle_state is None or row.lifecycle_state == lifecycle_state)
            and (strategy_instance_id is None or row.strategy_instance_id == strategy_instance_id)
            and (run_id is None or row.run_id == run_id)
        ]
        if after is not None:
            rows = [row for row in rows if (row.recorded_at_ms, row.journal_seq, row.transaction_id) < after]
        high_water = self.cursor.last_journal_seq if self.cursor else None
        lag = max((high_water or 0) - max((row.journal_seq for row in self.rows), default=0), 0) if high_water else None
        return [
            ClerkTransactionSummaryRow.model_validate(
                {
                    **row.model_dump(exclude={"receipt", "events", "custody_timeline"}),
                    "event_count": len(row.events),
                }
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
        self.reset_calls += 1
        self.rows = [row for row in self.rows if row.broker != broker]
        self.cursor = None
        self.feed = ("rebuilding", "Rebuild in progress", "Projection rebuild is replaying canonical Clerk acknowledgement evidence.", None, None, False)


class _CountingStore(_Store):
    """Production-shaped test store: the database retains rows, Python does not."""

    def __init__(self) -> None:
        super().__init__()
        self.projected_transactions = 0
        self.batch_count = 0
        self.max_batch_records = 0

    async def persist_batch(
        self, batch: ClerkTransactionBatch, *, updated_at_ms: int, allow_rebuilding: bool = False
    ) -> int:
        self.projected_transactions += len(batch.transactions)
        self.batch_count += 1
        self.max_batch_records = max(self.max_batch_records, batch.source_record_count)
        self.cursor = ClerkJournalCursor(
            batch.account_id,
            batch.journal_path,
            batch.next_byte_offset,
            batch.next_journal_seq,
            updated_at_ms,
            batch.journal_generation,
        )
        return len(batch.transactions)


@pytest.mark.asyncio
async def test_projection_pool_initialization_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    original_pool = clerk_transaction_projection_store._pool
    original_lock = clerk_transaction_projection_store._pool_init_lock
    clerk_transaction_projection_store._pool = None
    clerk_transaction_projection_store._pool_init_lock = None
    create_calls = 0
    created_pool = object()

    async def create_pool(*_args, **_kwargs) -> object:
        nonlocal create_calls
        create_calls += 1
        await asyncio.sleep(0)
        return created_pool

    monkeypatch.setattr(clerk_transaction_projection_store.settings, "CLERK_TRANSACTION_PROJECTION_ENABLED", True)
    monkeypatch.setattr(clerk_transaction_projection_store.settings, "POSTGRES_URL", "postgres://test")
    monkeypatch.setattr(clerk_transaction_projection_store.asyncpg, "create_pool", create_pool)
    try:
        await asyncio.gather(
            clerk_transaction_projection_store.init_pool(),
            clerk_transaction_projection_store.init_pool(),
        )
        assert create_calls == 1
        assert clerk_transaction_projection_store._pool is created_pool
    finally:
        clerk_transaction_projection_store._pool = original_pool
        clerk_transaction_projection_store._pool_init_lock = original_lock


@pytest.mark.asyncio
async def test_projection_store_allows_partial_fills_to_advance_to_filled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    _append(path, _callback(2, event_type="fill", status="Submitted"))
    _append(path, _callback(3, event_type="fill", status="Filled"))
    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)
    assert batch is not None

    class _Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _RecordingConnection:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def transaction(self) -> _Transaction:
            return _Transaction()

        async def execute(self, query: str, *_args: object) -> str:
            self.queries.append(query)
            if query.startswith("INSERT INTO clerk_transaction_events"):
                return "INSERT 0 1"
            return "INSERT 0 1"

        async def fetchval(self, *_args: object) -> None:
            return None

    connection = _RecordingConnection()

    async def _acquire_connection() -> _RecordingConnection:
        return connection

    async def _release_connection(released: _RecordingConnection) -> None:
        assert released is connection

    monkeypatch.setattr(clerk_transaction_projection_store.settings, "CLERK_TRANSACTION_PROJECTION_ENABLED", True)
    monkeypatch.setattr(clerk_transaction_projection_store.settings, "POSTGRES_URL", "postgres://test")
    monkeypatch.setattr(clerk_transaction_projection_store, "_connection", _acquire_connection)
    monkeypatch.setattr(clerk_transaction_projection_store, "_release_connection", _release_connection)

    await clerk_transaction_projection_store.PostgresClerkTransactionProjectionStore().persist_batch(
        batch,
        updated_at_ms=1_700_000_000_010,
    )

    lifecycle_updates = [
        query for query in connection.queries if query.startswith("UPDATE clerk_transactions")
    ]
    assert len(lifecycle_updates) == 3
    assert all("partially_filled" not in query for query in lifecycle_updates)


def _callback(
    seq: int,
    *,
    event_type: str,
    status: str | None,
    fee: float | None = None,
    error_code: int | None = None,
    callback_identity: str | None = None,
) -> AccountClerkJournalEntry:
    ack = _manual_ack(1)
    event = IbkrOrderEvent(
        account_id=ACCOUNT, order_id=ack.order_id or 0, perm_id=ack.perm_id,
        event_type=event_type, status=status, order_ref=ack.intent.order_ref if ack.intent else None,
        fill_quantity=1.0 if event_type == "fill" else None, remaining=0.0 if status == "Filled" else 1.0,
        fee=fee, ts_ms=1_700_000_000_000 + seq,
        error_code=error_code,
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
    assert store.feed[0] == "live"
    assert store.feed[3:] == (2, 0, False)


def test_read_appended_clerk_journal_projects_all_order_origins_from_recorded_stage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.jsonl"
    origins = (
        (MANUAL_ORDER_INTENT_KIND, "manual"),
        ("STRATEGY", "strategy"),
        ("RECOVERY_FLATTEN", "recovery"),
        ("EMERGENCY_FLATTEN", "emergency"),
    )
    for seq, (intent_kind, _) in enumerate(origins, start=1):
        _append(path, _intent_stage(seq, intent_kind, "recorded"))
    _append(path, _intent_stage(5, "CANCEL_NAMESPACE", "recorded"))

    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)

    assert batch is not None
    assert [row.transaction_origin for row in batch.transactions] == [origin for _, origin in origins]
    assert [event.event_kind for event in batch.events] == ["recorded"] * 4
    assert {row.transaction_kind for row in batch.transactions} == {
        "manual_ibkr_acknowledgement",
        "strategy_ibkr_order",
        "recovery_ibkr_order",
        "emergency_ibkr_order",
    }
    assert all(row.lifecycle_state == "recorded" for row in batch.transactions)
    assert batch.next_journal_seq == 5


def test_strategy_acknowledgement_and_callback_are_not_dropped_as_manual_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.jsonl"
    acknowledgement = _ack_for_intent_kind(1, "STRATEGY")
    callback = _callback(2, event_type="fill", status="Filled")
    _append(path, acknowledgement)
    _append(
        path,
        callback.model_copy(
            update={
                "intent": acknowledgement.intent,
                "broker_event": {
                    **(callback.broker_event or {}),
                    "order_ref": acknowledgement.intent.order_ref if acknowledgement.intent else None,
                },
            }
        ),
    )

    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)

    assert batch is not None
    assert {(row.transaction_origin, row.transaction_kind) for row in batch.transactions} == {
        ("strategy", "strategy_ibkr_order")
    }
    assert len({row.transaction_id for row in batch.transactions}) == 1
    assert [(event.event_kind, event.lifecycle_state) for event in batch.events] == [
        ("submitted", "submitted"),
        ("execution", "filled"),
    ]


def test_callback_before_acknowledgement_keeps_one_transaction_across_replay_windows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.jsonl"
    acknowledgement = _ack_for_intent_kind(2, "STRATEGY")
    raw_callback = _callback(1, event_type="fill", status="Filled")
    callback = raw_callback.model_copy(
        update={
            "intent": acknowledgement.intent,
            "broker_event": {
                **(raw_callback.broker_event or {}),
                "order_ref": acknowledgement.intent.order_ref if acknowledgement.intent else None,
            },
        }
    )
    _append(path, callback)
    _append(path, acknowledgement)
    first_record_size = path.read_bytes().index(b"\n") + 1

    first = read_appended_clerk_journal(
        account_id=ACCOUNT,
        journal_path=path,
        cursor=None,
        max_read_bytes=first_record_size,
    )
    assert first is not None
    second = read_appended_clerk_journal(
        account_id=ACCOUNT,
        journal_path=path,
        cursor=ClerkJournalCursor(
            ACCOUNT,
            str(path),
            first.next_byte_offset,
            first.next_journal_seq,
            1,
        ),
        max_read_bytes=first_record_size,
    )

    assert second is not None
    assert first.transactions[0].transaction_id == second.transactions[0].transaction_id
    assert first.transactions[0].lifecycle_state == "filled"
    assert second.transactions[0].lifecycle_state == "submitted"


@pytest.mark.asyncio
async def test_recorded_transaction_advances_through_submission_and_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.jsonl"
    acknowledgement = _ack_for_intent_kind(3, "STRATEGY")
    _append(
        path,
        acknowledgement.model_copy(
            update={"seq": 1, "entry_kind": "recorded", "order_id": None, "perm_id": None}
        ),
    )
    _append(
        path,
        acknowledgement.model_copy(
            update={"seq": 2, "entry_kind": "broker_submitting", "order_id": None, "perm_id": None}
        ),
    )
    _append(path, acknowledgement)
    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)
    assert batch is not None

    class _Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _RecordingConnection:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def transaction(self) -> _Transaction:
            return _Transaction()

        async def execute(self, query: str, *_args: object) -> str:
            self.queries.append(query)
            return "INSERT 0 1"

        async def fetchval(self, *_args: object) -> None:
            return None

    connection = _RecordingConnection()

    async def _acquire_connection() -> _RecordingConnection:
        return connection

    async def _release_connection(released: _RecordingConnection) -> None:
        assert released is connection

    monkeypatch.setattr(clerk_transaction_projection_store.settings, "CLERK_TRANSACTION_PROJECTION_ENABLED", True)
    monkeypatch.setattr(clerk_transaction_projection_store.settings, "POSTGRES_URL", "postgres://test")
    monkeypatch.setattr(clerk_transaction_projection_store, "_connection", _acquire_connection)
    monkeypatch.setattr(clerk_transaction_projection_store, "_release_connection", _release_connection)

    await clerk_transaction_projection_store.PostgresClerkTransactionProjectionStore().persist_batch(
        batch,
        updated_at_ms=1_700_000_000_010,
    )

    lifecycle_updates = [
        query for query in connection.queries if query.startswith("UPDATE clerk_transactions")
    ]
    assert len(lifecycle_updates) == 3


@pytest.mark.asyncio
async def test_repaired_journal_with_reused_sequence_gets_a_fresh_cursor_generation(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    store = _Store()
    await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=1)
    first_generation = store.cursor.journal_generation if store.cursor else ""

    repaired = path.with_suffix(".repaired")
    _append(repaired, _manual_ack(99).model_copy(update={"seq": 1}))
    repaired.replace(path)

    assert await tail_account_journal(
        artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=2
    ) == 1
    assert store.cursor is not None
    assert store.cursor.journal_generation != first_generation
    assert store.cursor.last_journal_seq == 1
    assert [row.intent_id for row in store.rows] == ["manual-1", "manual-99"]


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
async def test_normal_tail_cannot_restore_rows_during_a_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    store = _Store()
    await store.reset_projection(account_id=ACCOUNT, journal_path=str(path), broker="ibkr", updated_at_ms=1)

    with pytest.raises(ClerkTransactionProjectionUnavailable, match="rebuild is in progress"):
        await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=2)

    assert store.cursor is None
    assert store.rows == []


@pytest.mark.asyncio
async def test_rebuild_refuses_missing_journal_before_it_resets_the_projection(tmp_path: Path) -> None:
    store = _Store()

    with pytest.raises(ClerkTransactionProjectionUnavailable, match="reset was not started"):
        await rebuild_account_transaction_projection(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
            store=store,
            updated_at_ms=2,
        )

    assert store.reset_calls == 0


@pytest.mark.asyncio
async def test_recovery_does_not_replace_a_corruption_receipt_with_offline_status(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    store = _Store()
    store.feed = ("corrupt", "Projection corrupt", "Canonical evidence retained.", 1, None, False)
    assert await recover_account_transaction_feed(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        store=store,
        updated_at_ms=2,
    ) == 0

    assert store.feed[0] == "corrupt"
    assert store.persisted == []


@pytest.mark.asyncio
async def test_alpaca_recovery_requires_an_explicit_rebuild_to_clear_a_corruption_receipt(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "alpaca" / ACCOUNT / JOURNAL_FILENAME
    _append_alpaca(path, _alpaca_ack())
    store = _Store()
    store.feed = ("corrupt", "Projection corrupt", "Canonical evidence retained.", 1, None, False)

    assert await recover_alpaca_account_transaction_feed(
        artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=2
    ) == 0

    assert store.feed[0] == "corrupt"
    assert store.persisted == []


@pytest.mark.asyncio
async def test_normal_alpaca_tail_refreshes_feed_high_water_receipt(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "alpaca" / ACCOUNT / JOURNAL_FILENAME
    _append_alpaca(path, _alpaca_ack())
    store = _Store()

    assert await tail_alpaca_account_journal(
        artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=2
    ) == 1

    assert store.feed == (
        "live",
        "Live",
        "Durable Alpaca Clerk projection is current.",
        1,
        0,
        False,
    )


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
    monkeypatch.setattr(clerk_transaction_projection, "recover_account_transaction_feed", _fail)

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
    assert "custody_timeline" not in first.rows[0].model_dump()
    assert first.next_cursor is not None and first.next_cursor.startswith("ctxhp1.")
    second = await transaction_history(account_id=ACCOUNT, limit=2, cursor=first.next_cursor, store=store)
    assert [row.journal_seq for row in second.rows] == [1]


@pytest.mark.asyncio
async def test_history_filters_stay_in_the_account_scoped_projection(tmp_path: Path) -> None:
    store = _Store()
    path = tmp_path / "journal.jsonl"
    _append(path, _manual_ack(1))
    _append(path, _ack_for_intent_kind(2, "STRATEGY", 1_700_000_000_001))
    batch = read_appended_clerk_journal(account_id=ACCOUNT, journal_path=path, cursor=None)
    assert batch is not None
    await store.persist_batch(batch, updated_at_ms=1)

    page = await transaction_history(
        account_id=ACCOUNT,
        limit=25,
        cursor=None,
        origin="strategy",
        strategy_instance_id="bot-strategy",
        run_id="run-strategy",
        store=store,
    )

    assert [row.transaction_origin for row in page.rows] == ["strategy"]
    assert [row.intent_id for row in page.rows] == ["strategy-2"]


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


@pytest.mark.asyncio
async def test_callback_projection_distinguishes_ibkr_rejection_and_late_commission(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    _append(path, _callback(2, event_type="error", status="Inactive", error_code=201))
    _append(path, _callback(3, event_type="status", status="Submitted", fee=1.25))
    store = _Store()

    await tail_account_journal(artifacts_root=tmp_path, account_id=ACCOUNT, store=store)

    events = store.persisted[0].events
    assert [(event.event_kind, event.lifecycle_state) for event in events[1:]] == [
        ("rejected", "rejected"),
        ("commission", "submitted"),
    ]
    assert events[-1].commission_status == "reported"
    assert events[-1].fee == 1.25


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
async def test_recovery_commits_projection_and_marks_live(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    _append(path, _manual_ack(1))
    store = _Store()

    count = await recover_account_transaction_feed(
        artifacts_root=tmp_path, account_id=ACCOUNT, store=store, updated_at_ms=100
    )
    assert count == 1
    assert store.cursor is not None
    assert store.cursor.last_journal_seq == 1
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
