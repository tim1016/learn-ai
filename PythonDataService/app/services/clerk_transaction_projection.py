"""Dedicated Clerk-journal transaction projection.

The JSONL journal remains the acknowledgement authority.  This module only
tails complete newly-appended bytes and atomically commits its Postgres read
model and durable database cursor; it never writes the journal or invokes IBKR.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import asyncpg

from app.config import settings
from app.engine.live.account_artifacts import account_artifact_file_path
from app.engine.live.account_clerk_journal import ACCOUNT_CLERK_JOURNAL_FILENAME
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_owner import MANUAL_ORDER_INTENT_KIND
from app.schemas.clerk_transaction_projection import (
    ClerkTransactionEventRow,
    ClerkTransactionHistoryResponse,
    ClerkTransactionRow,
)

logger = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None


class ClerkTransactionProjectionUnavailable(RuntimeError):
    """The dedicated transaction projection cannot currently serve reads."""


@dataclass(frozen=True)
class ClerkJournalCursor:
    account_id: str
    journal_path: str
    last_byte_offset: int
    last_journal_seq: int
    updated_at_ms: int


@dataclass(frozen=True)
class ClerkTransactionBatch:
    account_id: str
    journal_path: str
    next_byte_offset: int
    next_journal_seq: int
    transactions: list[ClerkTransactionRow]
    events: list[ClerkTransactionEventRow]


class ClerkTransactionProjectionStore(Protocol):
    async def read_cursor(self, account_id: str, journal_path: str) -> ClerkJournalCursor | None: ...

    async def persist_batch(self, batch: ClerkTransactionBatch, *, updated_at_ms: int) -> int: ...

    async def history_page(
        self, *, account_id: str, limit: int, after: tuple[int, int, str] | None
    ) -> tuple[list[ClerkTransactionRow], int | None, int | None]: ...

    async def feed_status(self, account_id: str) -> tuple[str, str, str]: ...

    async def set_feed_status(
        self, account_id: str, state: str, headline: str, detail: str, *, updated_at_ms: int
    ) -> None: ...


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _ensure_configured() -> None:
    if not settings.CLERK_TRANSACTION_PROJECTION_ENABLED:
        raise ClerkTransactionProjectionUnavailable("CLERK_TRANSACTION_PROJECTION_ENABLED is false")
    if not settings.POSTGRES_URL:
        raise ClerkTransactionProjectionUnavailable("POSTGRES_URL is empty")


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _ensure_configured()
    _pool = await asyncpg.create_pool(settings.POSTGRES_URL, min_size=1, max_size=10, command_timeout=30)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _connection() -> asyncpg.Connection:
    if _pool is None:
        await init_pool()
    if _pool is None:
        raise ClerkTransactionProjectionUnavailable("asyncpg pool was not initialized")
    return await _pool.acquire()


def clerk_journal_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifact_file_path(artifacts_root, account_id, ACCOUNT_CLERK_JOURNAL_FILENAME)


def _opaque_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:32]}"


_TERMINAL_STATES = frozenset({"filled", "cancelled", "rejected", "error"})


def fold_lifecycle_state(current: str, incoming: str) -> str:
    """Apply one state-machine edge without allowing terminal regression."""

    return current if current in _TERMINAL_STATES else incoming


def _callback_state(entry: AccountClerkJournalEntry) -> tuple[str, str, str, float | None]:
    """Classify one raw IBKR callback; no consumer re-derives this lifecycle."""

    if entry.entry_kind == "broker_acked":
        return "submitted", "submitted", f"journal:{entry.seq}:broker_acked", None
    if entry.broker_event is None:
        raise ValueError("broker callback journal row is missing raw evidence")
    event = entry.broker_event
    callback = event.get("event_type")
    status = str(event.get("status") or "").lower()
    identity = entry.broker_callback_idempotency_key
    if not isinstance(identity, str) or not identity:
        raise ValueError("broker callback journal row is missing callback identity")
    fee = event.get("fee")
    if fee is not None and not isinstance(fee, int | float):
        raise ValueError("broker callback fee is not numeric")
    if callback == "fill":
        state = "filled" if status == "filled" or event.get("remaining") == 0 else "partially_filled"
        return "execution", state, identity, float(fee) if fee is not None else None
    if callback == "cancel" or status in {"cancelled", "apicancelled"}:
        return "cancelled", "cancelled", identity, None
    if callback == "error":
        state = "rejected" if status in {"inactive", "rejected"} else "error"
        return state, state, identity, None
    if status == "filled":
        return "filled", "filled", identity, None
    if status in {"partiallyfilled", "partialfilled"}:
        return "partial_fill", "partially_filled", identity, None
    if status in {"inactive", "rejected"}:
        return "rejected", "rejected", identity, None
    return "status", "submitted", identity, None


def _row_from_ack(account_id: str, entry: AccountClerkJournalEntry) -> tuple[ClerkTransactionRow, ClerkTransactionEventRow]:
    if entry.intent is None:
        raise ValueError("broker acknowledgement is missing its Clerk intent")
    transaction_id = _opaque_id("ctxn", account_id, entry.intent.order_ref)
    event_id = _opaque_id("ctxnev", account_id, entry.seq, "broker_acked")
    receipt = entry.model_dump(mode="json", exclude_none=True)
    event = ClerkTransactionEventRow(
        event_id=event_id,
        event_kind="submitted",
        callback_identity=f"journal:{entry.seq}:broker_acked",
        lifecycle_state="submitted",
        journal_seq=entry.seq,
        recorded_at_ms=entry.recorded_at_ms,
        receipt=receipt,
    )
    return (
        ClerkTransactionRow(
            transaction_id=transaction_id,
            account_id=account_id,
            journal_seq=entry.seq,
            recorded_at_ms=entry.recorded_at_ms,
            transaction_kind="manual_ibkr_acknowledgement",
            strategy_instance_id=entry.intent.strategy_instance_id,
            run_id=entry.intent.run_id,
            intent_id=entry.intent.intent_id,
            order_ref=entry.intent.order_ref,
            order_id=entry.order_id,
            perm_id=entry.perm_id,
            exec_id=entry.exec_id,
            lifecycle_state="submitted",
            receipt=receipt,
            events=[event],
        ),
        event,
    )


def _event_from_callback(
    account_id: str, entry: AccountClerkJournalEntry
) -> ClerkTransactionEventRow | None:
    if entry.intent is None or entry.intent.intent_kind != MANUAL_ORDER_INTENT_KIND:
        return None
    event_kind, lifecycle_state, identity, fee = _callback_state(entry)
    receipt = entry.model_dump(mode="json", exclude_none=True)
    return ClerkTransactionEventRow(
        event_id=_opaque_id("ctxnev", account_id, entry.seq, identity),
        event_kind=event_kind,
        callback_identity=identity,
        lifecycle_state=lifecycle_state,
        commission_status="reported" if fee is not None else "unknown",
        fee=fee,
        journal_seq=entry.seq,
        recorded_at_ms=entry.recorded_at_ms,
        receipt=receipt,
    )


def read_appended_clerk_journal(
    *, account_id: str, journal_path: Path, cursor: ClerkJournalCursor | None
) -> ClerkTransactionBatch | None:
    """Read only complete journal lines appended after the durable byte cursor."""

    offset = cursor.last_byte_offset if cursor is not None else 0
    last_seq = cursor.last_journal_seq if cursor is not None else 0
    if not journal_path.exists():
        return None
    with journal_path.open("rb") as journal_file:
        journal_file.seek(0, 2)
        journal_size = journal_file.tell()
        if offset > journal_size:
            raise ValueError("Clerk journal was replaced or truncated behind its projection cursor")
        journal_file.seek(offset)
        tail = journal_file.read()
    complete_size = tail.rfind(b"\n") + 1
    if complete_size == 0:
        return None
    consumed = tail[:complete_size]
    transactions: list[ClerkTransactionRow] = []
    events: list[ClerkTransactionEventRow] = []
    for line in consumed.splitlines():
        try:
            entry = AccountClerkJournalEntry.model_validate_json(line)
        except Exception as exc:
            raise ValueError("malformed complete Clerk journal record") from exc
        if entry.seq <= last_seq:
            raise ValueError("non-monotonic Clerk journal sequence after projection cursor")
        last_seq = entry.seq
        if entry.entry_kind == "broker_acked" and entry.intent is not None and entry.intent.intent_kind == MANUAL_ORDER_INTENT_KIND:
            transaction, event = _row_from_ack(account_id, entry)
            transactions.append(transaction)
            events.append(event)
        elif entry.entry_kind == "broker_event":
            event = _event_from_callback(account_id, entry)
            if event is not None:
                events.append(event)
    return ClerkTransactionBatch(
        account_id=account_id,
        journal_path=str(journal_path),
        next_byte_offset=offset + complete_size,
        next_journal_seq=last_seq,
        transactions=transactions,
        events=events,
    )


async def tail_account_journal(
    *, artifacts_root: Path, account_id: str, store: ClerkTransactionProjectionStore | None = None,
    updated_at_ms: int | None = None,
    publish: Callable[[ClerkTransactionBatch], Awaitable[None]] | None = None,
) -> int:
    """Project one bounded append-only journal tail. No broker or full scan occurs."""

    resolved_store = store or PostgresClerkTransactionProjectionStore()
    path = clerk_journal_path(artifacts_root, account_id)
    cursor = await resolved_store.read_cursor(account_id, str(path))
    batch = read_appended_clerk_journal(account_id=account_id, journal_path=path, cursor=cursor)
    if batch is None:
        return 0
    persisted = await resolved_store.persist_batch(batch, updated_at_ms=updated_at_ms or _now_ms())
    # Publishing is deliberately outside the committed transaction. A crash
    # before this point redelivers from the cursor; a crash after it can only
    # redeliver an already-idempotent semantic event.
    if publish is not None and (batch.transactions or batch.events):
        await publish(batch)
    return persisted


async def recover_account_transaction_feed(
    *, artifacts_root: Path, account_id: str, store: ClerkTransactionProjectionStore,
    publish: Callable[[ClerkTransactionBatch], Awaitable[None]] | None = None,
    updated_at_ms: int | None = None,
) -> int:
    """Drain bounded cursor lag before declaring the Clerk transaction feed live."""

    now = updated_at_ms or _now_ms()
    await store.set_feed_status(
        account_id, "reconnecting", "Reconnecting", "Replaying durable Clerk callbacks.", updated_at_ms=now
    )
    projected = 0
    try:
        while True:
            count = await tail_account_journal(
                artifacts_root=artifacts_root,
                account_id=account_id,
                store=store,
                updated_at_ms=now,
                publish=publish,
            )
            projected += count
            path = clerk_journal_path(artifacts_root, account_id)
            cursor = await store.read_cursor(account_id, str(path))
            pending = read_appended_clerk_journal(account_id=account_id, journal_path=path, cursor=cursor)
            if pending is None:
                break
        await store.set_feed_status(
            account_id,
            "live",
            "Live",
            "Durable Clerk callback projection is current.",
            updated_at_ms=now,
        )
        return projected
    except Exception:
        await store.set_feed_status(
            account_id,
            "offline_but_saved",
            "Offline but saved",
            "Projection is offline; canonical Clerk evidence is saved.",
            updated_at_ms=now,
        )
        raise


async def project_account_journal_best_effort(*, artifacts_root: Path, account_id: str) -> None:
    """Refresh the rebuildable projection without delaying a Clerk acknowledgement."""

    if not settings.CLERK_TRANSACTION_PROJECTION_ENABLED:
        return
    try:
        await tail_account_journal(artifacts_root=artifacts_root, account_id=account_id)
    except Exception:
        logger.exception(
            "Clerk transaction projection failed after durable acknowledgement",
            extra={"account_id": account_id},
        )


def _encode_cursor(value: tuple[int, int, str]) -> str:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return "ctxhp1." + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[int, int, str] | None:
    if value is None:
        return None
    if not value.startswith("ctxhp1."):
        raise ValueError("invalid transaction history cursor")
    try:
        decoded = base64.urlsafe_b64decode(value[7:] + "=" * (-len(value[7:]) % 4))
        timestamp, seq, transaction_id = json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid transaction history cursor") from exc
    if not isinstance(timestamp, int) or not isinstance(seq, int) or not isinstance(transaction_id, str):
        raise ValueError("invalid transaction history cursor")
    return timestamp, seq, transaction_id


async def transaction_history(
    *, account_id: str, limit: int, cursor: str | None, store: ClerkTransactionProjectionStore | None = None
) -> ClerkTransactionHistoryResponse:
    resolved_store = store or PostgresClerkTransactionProjectionStore()
    rows, high_water, lag = await resolved_store.history_page(
        account_id=account_id, limit=limit, after=_decode_cursor(cursor)
    )
    feed_state, feed_headline, feed_detail = await resolved_store.feed_status(account_id)
    next_cursor = (
        _encode_cursor((rows[-1].recorded_at_ms, rows[-1].journal_seq, rows[-1].transaction_id))
        if len(rows) == limit
        else None
    )
    return ClerkTransactionHistoryResponse(
        projection_available=True,
        canonical_fallback_required=False,
        feed_state=feed_state,  # type: ignore[arg-type]
        feed_headline=feed_headline,
        feed_detail=feed_detail,
        high_water_journal_seq=high_water,
        lag_records=lag,
        rows=rows,
        next_cursor=next_cursor,
    )


class PostgresClerkTransactionProjectionStore:
    """Postgres implementation; all write state is one database transaction."""

    async def read_cursor(self, account_id: str, journal_path: str) -> ClerkJournalCursor | None:
        _ensure_configured()
        conn = await _connection()
        try:
            record = await conn.fetchrow(
                "SELECT account_id, journal_path, last_byte_offset, last_journal_seq, updated_at_ms "
                "FROM clerk_transaction_projection_cursors WHERE account_id=$1 AND journal_path=$2",
                account_id, journal_path,
            )
        finally:
            assert _pool is not None
            await _pool.release(conn)
        return ClerkJournalCursor(**dict(record)) if record is not None else None

    async def persist_batch(self, batch: ClerkTransactionBatch, *, updated_at_ms: int) -> int:
        _ensure_configured()
        conn = await _connection()
        try:
            async with conn.transaction():
                for transaction in batch.transactions:
                    await conn.execute(
                        "INSERT INTO clerk_transactions (transaction_id, account_id, journal_seq, recorded_at_ms, transaction_kind, strategy_instance_id, run_id, intent_id, order_ref, order_id, perm_id, exec_id, lifecycle_state, commission_status, fee, receipt, inserted_at_ms, updated_at_ms) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb,$17,$17) "
                        "ON CONFLICT (transaction_id) DO NOTHING",
                        transaction.transaction_id, transaction.account_id, transaction.journal_seq, transaction.recorded_at_ms,
                        transaction.transaction_kind, transaction.strategy_instance_id, transaction.run_id, transaction.intent_id,
                        transaction.order_ref, transaction.order_id, transaction.perm_id, transaction.exec_id,
                        transaction.lifecycle_state, transaction.commission_status, transaction.fee,
                        json.dumps(transaction.receipt, sort_keys=True), updated_at_ms,
                    )
                for event in batch.events:
                    await conn.execute(
                        "INSERT INTO clerk_transaction_events (event_id, transaction_id, account_id, journal_seq, event_kind, callback_identity, lifecycle_state, commission_status, fee, recorded_at_ms, receipt, inserted_at_ms) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12) ON CONFLICT (account_id, journal_seq, callback_identity) DO NOTHING",
                        event.event_id, _event_transaction_id(batch.account_id, event), batch.account_id,
                        event.journal_seq, event.event_kind, event.callback_identity,
                        event.lifecycle_state, event.commission_status, event.fee,
                        event.recorded_at_ms, json.dumps(event.receipt, sort_keys=True), updated_at_ms,
                    )
                    if event.event_kind != "submitted":
                        await conn.execute(
                            "UPDATE clerk_transactions SET lifecycle_state=CASE WHEN lifecycle_state IN ('filled','cancelled','rejected','error') THEN lifecycle_state ELSE $3 END, commission_status=CASE WHEN $4='reported' THEN 'reported' ELSE commission_status END, fee=CASE WHEN $4='reported' THEN $5 ELSE fee END, updated_at_ms=$6 WHERE account_id=$1 AND order_ref=$2",
                            batch.account_id, _event_order_ref(event), event.lifecycle_state, event.commission_status, event.fee, updated_at_ms,
                        )
                await conn.execute(
                    "INSERT INTO clerk_transaction_projection_cursors (account_id, journal_path, last_byte_offset, last_journal_seq, updated_at_ms) VALUES ($1,$2,$3,$4,$5) "
                    "ON CONFLICT (account_id, journal_path) DO UPDATE SET last_byte_offset=EXCLUDED.last_byte_offset, last_journal_seq=EXCLUDED.last_journal_seq, updated_at_ms=EXCLUDED.updated_at_ms "
                    "WHERE (clerk_transaction_projection_cursors.last_byte_offset, clerk_transaction_projection_cursors.last_journal_seq) < (EXCLUDED.last_byte_offset, EXCLUDED.last_journal_seq)",
                    batch.account_id, batch.journal_path, batch.next_byte_offset, batch.next_journal_seq, updated_at_ms,
                )
        finally:
            assert _pool is not None
            await _pool.release(conn)
        return len(batch.transactions)

    async def history_page(
        self, *, account_id: str, limit: int, after: tuple[int, int, str] | None
    ) -> tuple[list[ClerkTransactionRow], int | None, int | None]:
        _ensure_configured()
        conn = await _connection()
        try:
            records = await conn.fetch(
                "SELECT t.transaction_id, t.account_id, t.journal_seq, t.recorded_at_ms, t.transaction_kind, t.strategy_instance_id, t.run_id, t.intent_id, t.order_ref, t.order_id, t.perm_id, t.exec_id, t.lifecycle_state, t.commission_status, t.fee, t.receipt, "
                "COALESCE(events.events, '[]'::jsonb) AS events "
                "FROM (SELECT transaction_id, account_id, journal_seq, recorded_at_ms, transaction_kind, strategy_instance_id, run_id, intent_id, order_ref, order_id, perm_id, exec_id, lifecycle_state, commission_status, fee, receipt "
                "FROM clerk_transactions WHERE account_id=$1 AND ($2::bigint IS NULL OR (recorded_at_ms, journal_seq, transaction_id) < ($2,$3,$4)) "
                "ORDER BY recorded_at_ms DESC, journal_seq DESC, transaction_id DESC LIMIT $5) t "
                "LEFT JOIN LATERAL (SELECT jsonb_agg(jsonb_build_object('event_id', e.event_id, 'event_kind', e.event_kind, 'callback_identity', e.callback_identity, 'lifecycle_state', e.lifecycle_state, 'commission_status', e.commission_status, 'fee', e.fee, 'journal_seq', e.journal_seq, 'recorded_at_ms', e.recorded_at_ms, 'receipt', e.receipt) ORDER BY e.journal_seq) AS events "
                "FROM clerk_transaction_events e WHERE e.transaction_id=t.transaction_id) events ON TRUE "
                "ORDER BY t.recorded_at_ms DESC, t.journal_seq DESC, t.transaction_id DESC",
                account_id, after[0] if after else None, after[1] if after else None, after[2] if after else None, limit,
            )
            metadata = await conn.fetchrow(
                "SELECT last_journal_seq AS high_water, 0::bigint AS lag "
                "FROM clerk_transaction_projection_cursors WHERE account_id=$1 "
                "ORDER BY updated_at_ms DESC LIMIT 1", account_id,
            )
        finally:
            assert _pool is not None
            await _pool.release(conn)
        rows = [
            ClerkTransactionRow.model_validate(
                {**dict(record), "receipt": _json_value(record["receipt"]), "events": _json_value(record["events"])}
            )
            for record in records
        ]
        return rows, (metadata["high_water"] if metadata else None), (metadata["lag"] if metadata else None)

    async def feed_status(self, account_id: str) -> tuple[str, str, str]:
        _ensure_configured()
        conn = await _connection()
        try:
            record = await conn.fetchrow(
                "SELECT state, headline, detail FROM clerk_transaction_feed_status WHERE account_id=$1", account_id
            )
        finally:
            assert _pool is not None
            await _pool.release(conn)
        return (
            (str(record["state"]), str(record["headline"]), str(record["detail"]))
            if record
            else ("stale", "Stale", "No current projection-feed receipt.")
        )

    async def set_feed_status(
        self, account_id: str, state: str, headline: str, detail: str, *, updated_at_ms: int
    ) -> None:
        _ensure_configured()
        conn = await _connection()
        try:
            await conn.execute(
                "INSERT INTO clerk_transaction_feed_status (account_id, state, headline, detail, updated_at_ms) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (account_id) DO UPDATE SET "
                "state=EXCLUDED.state, headline=EXCLUDED.headline, detail=EXCLUDED.detail, "
                "updated_at_ms=EXCLUDED.updated_at_ms",
                account_id,
                state,
                headline,
                detail,
                updated_at_ms,
            )
        finally:
            assert _pool is not None
            await _pool.release(conn)


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _event_order_ref(event: ClerkTransactionEventRow) -> str:
    broker_event = event.receipt.get("broker_event")
    if not isinstance(broker_event, dict) or not isinstance(broker_event.get("order_ref"), str):
        raise ValueError("callback event has no opaque order_ref")
    return broker_event["order_ref"]


def _event_transaction_id(account_id: str, event: ClerkTransactionEventRow) -> str:
    if event.event_kind == "submitted":
        intent = event.receipt.get("intent")
        if isinstance(intent, dict) and isinstance(intent.get("order_ref"), str):
            return _opaque_id("ctxn", account_id, intent["order_ref"])
    return _opaque_id("ctxn", account_id, _event_order_ref(event))


__all__ = [
    "ClerkJournalCursor", "ClerkTransactionBatch", "ClerkTransactionProjectionStore",
    "ClerkTransactionProjectionUnavailable", "PostgresClerkTransactionProjectionStore",
    "clerk_journal_path", "fold_lifecycle_state", "project_account_journal_best_effort",
    "read_appended_clerk_journal", "recover_account_transaction_feed", "tail_account_journal",
    "transaction_history",
]
