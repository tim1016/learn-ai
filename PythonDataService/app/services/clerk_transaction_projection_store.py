"""Postgres persistence for the rebuildable Clerk transaction projection."""

from __future__ import annotations

import asyncio
import json
from typing import cast

import asyncpg

from app.config import settings
from app.schemas.clerk_transaction_projection import (
    TRANSACTION_FEED_STATES,
    ClerkTransactionRow,
    ClerkTransactionSummaryRow,
    TransactionFeedState,
    TransactionOrigin,
)
from app.services.clerk_transaction_projection import (
    ClerkJournalCursor,
    ClerkTransactionBatch,
    ClerkTransactionProjectionUnavailable,
    _json_value,
)
from app.services.clerk_transaction_projection_ibkr import (
    event_order_ref,
    event_transaction_id,
)

_pool: asyncpg.Pool | None = None
_pool_init_lock: asyncio.Lock | None = None


def _ensure_configured() -> None:
    if not settings.CLERK_TRANSACTION_PROJECTION_ENABLED:
        raise ClerkTransactionProjectionUnavailable("CLERK_TRANSACTION_PROJECTION_ENABLED is false")
    if not settings.POSTGRES_URL:
        raise ClerkTransactionProjectionUnavailable("POSTGRES_URL is empty")


async def init_pool() -> None:
    """Create the Clerk projection pool once for projection reads and writes."""

    global _pool, _pool_init_lock
    if _pool is not None:
        return
    if _pool_init_lock is None:
        _pool_init_lock = asyncio.Lock()
    async with _pool_init_lock:
        if _pool is not None:
            return
        _ensure_configured()
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )


async def close_pool() -> None:
    """Close the Clerk projection pool when the service shuts down."""

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


async def _release_connection(conn: asyncpg.Connection) -> None:
    if _pool is None:
        raise ClerkTransactionProjectionUnavailable("asyncpg pool was not initialized")
    await _pool.release(conn)


class PostgresClerkTransactionProjectionStore:
    """Postgres implementation; all projection writes are transactional."""

    async def read_cursor(self, account_id: str, journal_path: str) -> ClerkJournalCursor | None:
        _ensure_configured()
        conn = await _connection()
        try:
            record = await conn.fetchrow(
                "SELECT account_id, journal_path, last_byte_offset, last_journal_seq, updated_at_ms, journal_generation "
                "FROM clerk_transaction_projection_cursors WHERE account_id=$1 AND journal_path=$2",
                account_id,
                journal_path,
            )
        finally:
            await _release_connection(conn)
        return ClerkJournalCursor(**dict(record)) if record is not None else None

    async def persist_batch(
        self, batch: ClerkTransactionBatch, *, updated_at_ms: int, allow_rebuilding: bool = False
    ) -> int:
        _ensure_configured()
        conn = await _connection()
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", batch.journal_path)
                feed_state = await conn.fetchval(
                    "SELECT state FROM clerk_transaction_feed_status WHERE account_id=$1 FOR KEY SHARE",
                    batch.account_id,
                )
                if feed_state == "rebuilding" and not allow_rebuilding:
                    raise ClerkTransactionProjectionUnavailable("transaction projection rebuild is in progress")
                for transaction in batch.transactions:
                    await conn.execute(
                        "INSERT INTO clerk_transactions (transaction_id, broker, account_id, journal_generation, journal_seq, recorded_at_ms, transaction_kind, transaction_origin, order_instruction, strategy_instance_id, run_id, intent_id, order_ref, order_id, perm_id, exec_id, native_order_id, native_execution_id, lifecycle_state, commission_status, fee, receipt, inserted_at_ms, updated_at_ms) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22::jsonb,$23,$23) "
                        "ON CONFLICT (transaction_id) DO UPDATE SET order_id=COALESCE(EXCLUDED.order_id, clerk_transactions.order_id), perm_id=COALESCE(EXCLUDED.perm_id, clerk_transactions.perm_id), exec_id=COALESCE(EXCLUDED.exec_id, clerk_transactions.exec_id), native_order_id=COALESCE(EXCLUDED.native_order_id, clerk_transactions.native_order_id), native_execution_id=COALESCE(EXCLUDED.native_execution_id, clerk_transactions.native_execution_id), updated_at_ms=EXCLUDED.updated_at_ms",
                        transaction.transaction_id, transaction.broker, transaction.account_id, batch.journal_generation, transaction.journal_seq, transaction.recorded_at_ms,
                        transaction.transaction_kind, transaction.transaction_origin, json.dumps(transaction.order_instruction.model_dump(mode="json"), sort_keys=True),
                        transaction.strategy_instance_id, transaction.run_id, transaction.intent_id,
                        transaction.order_ref, transaction.order_id, transaction.perm_id, transaction.exec_id,
                        transaction.native_order_id, transaction.native_execution_id, transaction.lifecycle_state,
                        transaction.commission_status, transaction.fee, json.dumps(transaction.receipt, sort_keys=True), updated_at_ms,
                    )
                for event in batch.events:
                    event_inserted = await conn.execute(
                        "INSERT INTO clerk_transaction_events (event_id, transaction_id, broker, account_id, journal_generation, journal_seq, event_kind, callback_identity, lifecycle_state, native_order_id, native_execution_id, commission_status, fee, recorded_at_ms, receipt, inserted_at_ms) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb,$16) ON CONFLICT (broker, account_id, callback_identity) WHERE callback_identity <> '' DO NOTHING",
                        event.event_id, event_transaction_id(batch.account_id, event), event.broker, batch.account_id, batch.journal_generation,
                        event.journal_seq, event.event_kind, event.callback_identity, event.lifecycle_state,
                        event.native_order_id, event.native_execution_id, event.commission_status, event.fee,
                        event.recorded_at_ms, json.dumps(event.receipt, sort_keys=True), updated_at_ms,
                    )
                    if (
                        event_inserted == "INSERT 0 1"
                        and not event.event_kind.startswith("activity_")
                    ):
                        await conn.execute(
                            "UPDATE clerk_transactions SET lifecycle_state=CASE WHEN lifecycle_state IN ('filled','cancelled','rejected','error') THEN lifecycle_state ELSE $4 END, commission_status=CASE WHEN $5='reported' THEN 'reported' ELSE commission_status END, fee=CASE WHEN $5='reported' THEN $6 ELSE fee END, updated_at_ms=$7 WHERE broker=$1 AND account_id=$2 AND order_ref=$3",
                            event.broker, batch.account_id, event_order_ref(event), event.lifecycle_state,
                            event.commission_status, event.fee, updated_at_ms,
                        )
                await conn.execute(
                    "INSERT INTO clerk_transaction_projection_cursors (account_id, journal_path, last_byte_offset, last_journal_seq, updated_at_ms, journal_generation) VALUES ($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT (account_id, journal_path) DO UPDATE SET last_byte_offset=EXCLUDED.last_byte_offset, last_journal_seq=EXCLUDED.last_journal_seq, updated_at_ms=EXCLUDED.updated_at_ms, journal_generation=EXCLUDED.journal_generation "
                    "WHERE clerk_transaction_projection_cursors.journal_generation <> EXCLUDED.journal_generation OR (clerk_transaction_projection_cursors.last_byte_offset, clerk_transaction_projection_cursors.last_journal_seq) < (EXCLUDED.last_byte_offset, EXCLUDED.last_journal_seq)",
                    batch.account_id, batch.journal_path, batch.next_byte_offset, batch.next_journal_seq, updated_at_ms, batch.journal_generation,
                )
        finally:
            await _release_connection(conn)
        return len({transaction.transaction_id for transaction in batch.transactions})

    async def history_page(
        self,
        *,
        account_id: str,
        limit: int,
        after: tuple[int, int, str] | None,
        origin: TransactionOrigin | None,
        lifecycle_state: str | None,
        strategy_instance_id: str | None,
        run_id: str | None,
        from_ms: int | None = None,
        to_ms: int | None = None,
    ) -> tuple[list[ClerkTransactionSummaryRow], int | None, int | None]:
        _ensure_configured()
        conn = await _connection()
        try:
            records = await conn.fetch(
                "SELECT t.transaction_id, t.broker, t.account_id, t.journal_seq, t.recorded_at_ms, t.transaction_kind, t.transaction_origin, t.order_instruction, t.strategy_instance_id, t.run_id, t.intent_id, t.order_ref, t.order_id, t.perm_id, t.exec_id, t.native_order_id, t.native_execution_id, t.lifecycle_state, t.commission_status, t.fee, "
                "(SELECT COUNT(*)::integer FROM clerk_transaction_events e WHERE e.transaction_id=t.transaction_id) AS event_count "
                "FROM clerk_transactions t WHERE t.account_id=$1 AND ($2::bigint IS NULL OR (t.recorded_at_ms, t.journal_seq, t.transaction_id) < ($2,$3,$4)) AND ($6::text IS NULL OR t.transaction_origin=$6) AND ($7::text IS NULL OR t.lifecycle_state=$7) AND ($8::text IS NULL OR t.strategy_instance_id=$8) AND ($9::text IS NULL OR t.run_id=$9) AND ($10::bigint IS NULL OR t.recorded_at_ms >= $10) AND ($11::bigint IS NULL OR t.recorded_at_ms <= $11) "
                "ORDER BY t.recorded_at_ms DESC, t.journal_seq DESC, t.transaction_id DESC LIMIT $5",
                account_id,
                after[0] if after else None,
                after[1] if after else None,
                after[2] if after else None,
                limit,
                origin,
                lifecycle_state,
                strategy_instance_id,
                run_id,
                from_ms,
                to_ms,
            )
            metadata = await conn.fetchrow(
                "SELECT last_journal_seq AS high_water, NULL::bigint AS lag FROM clerk_transaction_projection_cursors WHERE account_id=$1 ORDER BY updated_at_ms DESC LIMIT 1",
                account_id,
            )
        finally:
            await _release_connection(conn)
        rows = [
            ClerkTransactionSummaryRow.model_validate(
                {**dict(record), "order_instruction": _json_value(record["order_instruction"])}
            )
            for record in records
        ]
        return rows, (metadata["high_water"] if metadata else None), (metadata["lag"] if metadata else None)

    async def transaction_detail(self, *, account_id: str, transaction_id: str) -> ClerkTransactionRow | None:
        _ensure_configured()
        conn = await _connection()
        try:
            record = await conn.fetchrow(
                "SELECT t.transaction_id, t.broker, t.account_id, t.journal_seq, t.recorded_at_ms, t.transaction_kind, t.transaction_origin, t.order_instruction, t.strategy_instance_id, t.run_id, t.intent_id, t.order_ref, t.order_id, t.perm_id, t.exec_id, t.native_order_id, t.native_execution_id, t.lifecycle_state, t.commission_status, t.fee, t.receipt, "
                "COALESCE(jsonb_agg(jsonb_build_object('event_id', e.event_id, 'broker', e.broker, 'event_kind', e.event_kind, 'callback_identity', e.callback_identity, 'lifecycle_state', e.lifecycle_state, 'native_order_id', e.native_order_id, 'native_execution_id', e.native_execution_id, 'commission_status', e.commission_status, 'fee', e.fee, 'journal_seq', e.journal_seq, 'recorded_at_ms', e.recorded_at_ms, 'receipt', e.receipt) ORDER BY e.journal_seq) FILTER (WHERE e.event_id IS NOT NULL), '[]'::jsonb) AS events "
                "FROM clerk_transactions t LEFT JOIN clerk_transaction_events e ON e.transaction_id=t.transaction_id WHERE t.account_id=$1 AND t.transaction_id=$2 GROUP BY t.id",
                account_id,
                transaction_id,
            )
        finally:
            await _release_connection(conn)
        if record is None:
            return None
        return ClerkTransactionRow.model_validate(
            {
                **dict(record),
                "order_instruction": _json_value(record["order_instruction"]),
                "receipt": _json_value(record["receipt"]),
                "events": _json_value(record["events"]),
            }
        )

    async def feed_status(
        self, account_id: str
    ) -> tuple[TransactionFeedState, str, str, int | None, int | None, bool]:
        _ensure_configured()
        conn = await _connection()
        try:
            record = await conn.fetchrow(
                "SELECT state, headline, detail, high_water_journal_seq, lag_records, lag_is_lower_bound FROM clerk_transaction_feed_status WHERE account_id=$1",
                account_id,
            )
        finally:
            await _release_connection(conn)
        if record is None:
            return "stale", "Stale", "No current projection-feed receipt.", None, None, False
        state = str(record["state"])
        if state not in TRANSACTION_FEED_STATES:
            raise ClerkTransactionProjectionUnavailable("stored transaction feed state is invalid")
        return cast(TransactionFeedState, state), str(record["headline"]), str(record["detail"]), record["high_water_journal_seq"], record["lag_records"], bool(record["lag_is_lower_bound"])

    async def set_feed_status(
        self, account_id: str, state: TransactionFeedState, headline: str, detail: str, *, updated_at_ms: int,
        high_water_journal_seq: int | None = None, lag_records: int | None = None, lag_is_lower_bound: bool = False,
    ) -> None:
        _ensure_configured()
        conn = await _connection()
        try:
            await conn.execute(
                "INSERT INTO clerk_transaction_feed_status (account_id, state, headline, detail, updated_at_ms, high_water_journal_seq, lag_records, lag_is_lower_bound) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (account_id) DO UPDATE SET state=EXCLUDED.state, headline=EXCLUDED.headline, detail=EXCLUDED.detail, updated_at_ms=EXCLUDED.updated_at_ms, high_water_journal_seq=EXCLUDED.high_water_journal_seq, lag_records=EXCLUDED.lag_records, lag_is_lower_bound=EXCLUDED.lag_is_lower_bound",
                account_id, state, headline, detail, updated_at_ms, high_water_journal_seq, lag_records, lag_is_lower_bound,
            )
        finally:
            await _release_connection(conn)

    async def reset_projection(self, *, account_id: str, journal_path: str, broker: str, updated_at_ms: int) -> None:
        """Start one account rebuild under the journal lock without touching canonical evidence."""

        _ensure_configured()
        conn = await _connection()
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", journal_path)
                current_state = await conn.fetchval(
                    "SELECT state FROM clerk_transaction_feed_status WHERE account_id=$1 FOR UPDATE", account_id
                )
                if current_state == "rebuilding":
                    raise ClerkTransactionProjectionUnavailable("transaction projection rebuild is already in progress")
                await conn.execute("DELETE FROM clerk_transaction_events WHERE account_id=$1 AND broker=$2", account_id, broker)
                await conn.execute("DELETE FROM clerk_transactions WHERE account_id=$1 AND broker=$2", account_id, broker)
                await conn.execute(
                    "DELETE FROM clerk_transaction_projection_cursors WHERE account_id=$1 AND journal_path=$2", account_id, journal_path
                )
                await conn.execute(
                    "INSERT INTO clerk_transaction_feed_status (account_id, state, headline, detail, updated_at_ms, high_water_journal_seq, lag_records, lag_is_lower_bound) VALUES ($1,'rebuilding','Rebuild in progress','Projection rebuild is replaying canonical Clerk acknowledgement evidence.',$2,NULL,NULL,FALSE) ON CONFLICT (account_id) DO UPDATE SET state=EXCLUDED.state, headline=EXCLUDED.headline, detail=EXCLUDED.detail, updated_at_ms=EXCLUDED.updated_at_ms, high_water_journal_seq=NULL, lag_records=NULL, lag_is_lower_bound=FALSE",
                    account_id,
                    updated_at_ms,
                )
        finally:
            await _release_connection(conn)
