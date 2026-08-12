"""Dedicated Clerk-journal transaction projection.

The JSONL journal remains the acknowledgement authority.  This module only
tails complete newly-appended bytes and atomically commits its Postgres read
model and durable database cursor; it never writes the journal or invokes IBKR.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from app.broker.alpaca.clerk.journal import JOURNAL_FILENAME
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.config import settings
from app.engine.live.account_artifacts import account_artifact_file_path
from app.engine.live.account_clerk_journal import ACCOUNT_CLERK_JOURNAL_FILENAME
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.schemas.clerk_transaction_projection import (
    ClerkCustodyWindowSummary,
    ClerkOrderInstruction,
    ClerkTransactionEventRow,
    ClerkTransactionHistoryResponse,
    ClerkTransactionRow,
    ClerkTransactionSummaryRow,
    TransactionFeedState,
    TransactionOrigin,
)
from app.services.clerk_custody_timeline import custody_timeline_from_transaction
from app.services.clerk_transaction_projection_ibkr import (
    event_from_journal,
    opaque_projection_id,
    row_from_event,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)
MAX_PROJECTION_READ_BYTES = 1_048_576
MAX_RECOVERY_BATCHES = 64
MAX_JOURNAL_TAIL_RECORDS = 500

_CUSTODY_STAGE_BY_LIFECYCLE: dict[str, str] = {
    "recorded": "a0",
    "queued": "a0",
    "custody_accepted": "a0",
    "submission_hold": "a0",
    "submitting": "a1",
    "broker_write_started": "a1",
    "broker_known": "a2",
    # Both IBKR's durable broker_acked receipt and Alpaca's SUBMIT_ACKED
    # projection present as submitted. That is broker-known, not an unknown
    # client-side guess; callbacks may still arrive later or out of order.
    "submitted": "a2",
    "acknowledged": "a2",
    "partial_fill": "a2",
    "partially_filled": "a2",
    "economic_terminal": "a3",
    "expired_before_submit": "a3",
    "filled": "a3",
    "cancelled": "a3",
    "rejected": "a3",
    # An IBKR error is not terminal unless the callback itself proves rejection.
    # The projector preserves that distinction as ``error``; show it as uncertain
    # rather than claiming the economic lifecycle is complete.
    "error": "uncertain",
}

if TYPE_CHECKING:
    from app.services.clerk_transaction_projection_store import PostgresClerkTransactionProjectionStore


class ClerkTransactionProjectionUnavailable(RuntimeError):
    """The dedicated transaction projection cannot currently serve reads."""


class ClerkTransactionProjectionCorrupt(ClerkTransactionProjectionUnavailable):
    """A complete canonical record cannot safely be projected."""


@dataclass(frozen=True)
class ClerkJournalCursor:
    account_id: str
    journal_path: str
    last_byte_offset: int
    last_journal_seq: int
    updated_at_ms: int
    journal_generation: str = ""


@dataclass(frozen=True)
class ClerkTransactionBatch:
    account_id: str
    journal_path: str
    next_byte_offset: int
    next_journal_seq: int
    transactions: list[ClerkTransactionRow]
    events: list[ClerkTransactionEventRow]
    source_record_count: int
    has_more_complete_records: bool
    journal_generation: str = ""


class ClerkTransactionProjectionStore(Protocol):
    async def read_cursor(self, account_id: str, journal_path: str) -> ClerkJournalCursor | None: ...

    async def persist_batch(
        self, batch: ClerkTransactionBatch, *, updated_at_ms: int, allow_rebuilding: bool = False
    ) -> int: ...

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
    ) -> tuple[list[ClerkTransactionSummaryRow], int | None, int | None]: ...

    async def transaction_detail(
        self, *, account_id: str, transaction_id: str
    ) -> ClerkTransactionRow | None: ...

    async def feed_status(
        self, account_id: str
    ) -> tuple[TransactionFeedState, str, str, int | None, int | None, bool]: ...

    async def set_feed_status(
        self,
        account_id: str,
        state: TransactionFeedState,
        headline: str,
        detail: str,
        *,
        updated_at_ms: int,
        high_water_journal_seq: int | None = None,
        lag_records: int | None = None,
        lag_is_lower_bound: bool = False,
    ) -> None: ...

    async def reset_projection(
        self, *, account_id: str, journal_path: str, broker: str, updated_at_ms: int
    ) -> None: ...


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _default_store() -> ClerkTransactionProjectionStore:
    """Delay the database adapter import so journal parsing stays dependency-light."""

    from app.services.clerk_transaction_projection_store import PostgresClerkTransactionProjectionStore

    return PostgresClerkTransactionProjectionStore()


def clerk_journal_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifact_file_path(artifacts_root, account_id, ACCOUNT_CLERK_JOURNAL_FILENAME)


def alpaca_clerk_journal_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the canonical Alpaca Clerk ledger, never an IBKR artifact path."""

    return artifacts_root / "accounts" / "alpaca" / account_id / JOURNAL_FILENAME


def _journal_generation(journal_path: Path) -> str | None:
    """Identify one canonical journal incarnation without parsing its contents."""

    try:
        stat = journal_path.stat()
    except FileNotFoundError:
        return None
    return f"{stat.st_dev:x}:{stat.st_ino:x}"


_TERMINAL_STATES = frozenset({"filled", "cancelled", "rejected", "error"})


def fold_lifecycle_state(current: str, incoming: str) -> str:
    """Apply one state-machine edge without allowing terminal regression."""

    return current if current in _TERMINAL_STATES else incoming


def _alpaca_lifecycle_state(kind: ClerkEntryKind, entry: OrderJournalEntry) -> str:
    """Map only Alpaca's documented journal vocabulary; never reuse IBKR callbacks."""

    if kind is ClerkEntryKind.SUBMIT_ACKED:
        return "submitted"
    event = entry.event.event_type if entry.event is not None else ""
    return {
        "fill": "filled",
        "partial_fill": "partially_filled",
        "canceled": "cancelled",
        "expired": "cancelled",
        "replaced": "cancelled",
        "rejected": "rejected",
    }.get(event, "submitted")


def _alpaca_transaction_id(account_id: str, order_ref: str) -> str:
    return opaque_projection_id("ctxn", "alpaca", account_id, order_ref)


def _row_from_alpaca_ack(
    account_id: str, journal_seq: int, entry: OrderJournalEntry
) -> tuple[ClerkTransactionRow, ClerkTransactionEventRow]:
    """Project an Alpaca acknowledgement without assigning IBKR callback meaning."""

    if entry.order is None or not entry.order_ref:
        raise ValueError("Alpaca submit acknowledgement is missing durable identity or order")
    receipt = entry.model_dump(mode="json", exclude_none=True)
    transaction_id = _alpaca_transaction_id(account_id, entry.order_ref)
    event = ClerkTransactionEventRow(
        event_id=opaque_projection_id("ctxnev", "alpaca", account_id, journal_seq, "submit_acked"),
        broker="alpaca",
        event_kind="submitted",
        callback_identity=f"alpaca-journal:{journal_seq}:submit_acked",
        lifecycle_state=_alpaca_lifecycle_state(entry.kind, entry),
        native_order_id=entry.order.order_id,
        journal_seq=journal_seq,
        recorded_at_ms=entry.recorded_at_ms,
        receipt=receipt,
    )
    return (
        ClerkTransactionRow(
            transaction_id=transaction_id,
            broker="alpaca",
            account_id=account_id,
            journal_seq=journal_seq,
            recorded_at_ms=entry.recorded_at_ms,
            transaction_kind="manual_alpaca_acknowledgement",
            transaction_origin="manual",
            order_instruction=ClerkOrderInstruction(
                symbol=entry.leg.symbol if entry.leg is not None else entry.order.symbol,
                action=entry.leg.side if entry.leg is not None else entry.order.side,
                quantity=entry.leg.quantity if entry.leg is not None else entry.order.quantity,
                order_type=entry.leg.order_type if entry.leg is not None else entry.order.order_type,
                limit_price=entry.leg.limit_price if entry.leg is not None else entry.order.limit_price,
                time_in_force=(
                    entry.leg.time_in_force if entry.leg is not None else entry.order.time_in_force
                ),
            ),
            strategy_instance_id=f"manual/{entry.operator}",
            run_id="manual_operator",
            intent_id=entry.intent_id,
            order_ref=entry.order_ref,
            native_order_id=entry.order.order_id,
            lifecycle_state=_alpaca_lifecycle_state(entry.kind, entry),
            receipt=receipt,
            events=[event],
        ),
        event,
    )


def _event_from_alpaca_journal(
    account_id: str, journal_seq: int, entry: OrderJournalEntry
) -> ClerkTransactionEventRow | None:
    if not entry.order_ref:
        return None
    if entry.kind is ClerkEntryKind.ACTIVITY_RECOVERY:
        if entry.activity is None:
            return None
        receipt = entry.model_dump(mode="json", exclude_none=True)
        return ClerkTransactionEventRow(
            event_id=opaque_projection_id("ctxnev", "alpaca", account_id, journal_seq, entry.activity.activity_id),
            broker="alpaca",
            event_kind=f"activity_{entry.activity.activity_type.lower()}",
            callback_identity=f"alpaca-activity:{entry.activity.activity_id}",
            lifecycle_state="recovered_activity",
            native_order_id=entry.activity.native_order_id,
            journal_seq=journal_seq,
            recorded_at_ms=entry.recorded_at_ms,
            receipt=receipt,
        )
    if entry.kind is ClerkEntryKind.CANCEL_ACKED:
        receipt = entry.model_dump(mode="json", exclude_none=True)
        return ClerkTransactionEventRow(
            event_id=opaque_projection_id("ctxnev", "alpaca", account_id, journal_seq, "cancel_acked"),
            broker="alpaca",
            event_kind="cancelled",
            callback_identity=f"alpaca-journal:{journal_seq}:cancel_acked",
            lifecycle_state="cancelled",
            native_order_id=entry.broker_order_id,
            journal_seq=journal_seq,
            recorded_at_ms=entry.recorded_at_ms,
            receipt=receipt,
        )
    if entry.kind is not ClerkEntryKind.ORDER_EVENT or entry.event is None:
        return None
    receipt = entry.model_dump(mode="json", exclude_none=True)
    identity = entry.event_key or f"alpaca-journal:{journal_seq}:order_event"
    return ClerkTransactionEventRow(
        event_id=opaque_projection_id("ctxnev", "alpaca", account_id, journal_seq, identity),
        broker="alpaca",
        event_kind=entry.event.event_type,
        callback_identity=identity,
        lifecycle_state=_alpaca_lifecycle_state(entry.kind, entry),
        native_order_id=entry.broker_order_id,
        native_execution_id=(identity[5:] if identity.startswith("exec:") else None),
        journal_seq=journal_seq,
        recorded_at_ms=entry.recorded_at_ms,
        receipt=receipt,
    )


def _read_bounded_complete_tail(
    *, journal_path: Path, offset: int, broker: str, max_read_bytes: int
) -> tuple[bytes, bool] | None:
    """Return a bounded set of whole JSONL records without repairing input."""

    if max_read_bytes < 1:
        raise ValueError("max_read_bytes must be positive")
    if not journal_path.exists():
        return None
    with journal_path.open("rb") as journal_file:
        journal_file.seek(0, 2)
        journal_size = journal_file.tell()
        if offset > journal_size:
            raise ClerkTransactionProjectionCorrupt(
                f"{broker} Clerk journal was replaced or truncated behind its projection cursor"
            )
        journal_file.seek(offset)
        window = journal_file.read(max_read_bytes + 1)
    if not window:
        return None
    bounded = window[:max_read_bytes]
    consumed_end = 0
    for _ in range(MAX_JOURNAL_TAIL_RECORDS):
        newline_index = bounded.find(b"\n", consumed_end)
        if newline_index < 0:
            break
        consumed_end = newline_index + 1
    if consumed_end == 0:
        if len(window) > max_read_bytes:
            raise ClerkTransactionProjectionCorrupt(
                f"{broker} Clerk journal record exceeds the projection read bound"
            )
        return None
    return bounded[:consumed_end], consumed_end < len(window)


def _has_complete_appended_record(*, journal_path: Path, offset: int, broker: str) -> bool:
    """Return whether a later bounded tail contains a complete record.

    Recovery needs this only to decide whether to schedule another bounded
    projection pass.  Parsing that next record here would validate it twice:
    once for the look-ahead and again when the next tail projects it.
    """

    return (
        _read_bounded_complete_tail(
            journal_path=journal_path,
            offset=offset,
            broker=broker,
            max_read_bytes=MAX_PROJECTION_READ_BYTES,
        )
        is not None
    )


def read_appended_clerk_journal(
    *,
    account_id: str,
    journal_path: Path,
    cursor: ClerkJournalCursor | None,
    max_read_bytes: int | None = None,
) -> ClerkTransactionBatch | None:
    """Read one bounded set of complete Clerk records after the durable cursor."""

    offset = cursor.last_byte_offset if cursor is not None else 0
    last_seq = cursor.last_journal_seq if cursor is not None else 0
    bounded = _read_bounded_complete_tail(
        journal_path=journal_path,
        offset=offset,
        broker="IBKR",
        max_read_bytes=MAX_PROJECTION_READ_BYTES if max_read_bytes is None else max_read_bytes,
    )
    if bounded is None:
        return None
    consumed, has_more = bounded
    transactions: list[ClerkTransactionRow] = []
    events: list[ClerkTransactionEventRow] = []
    lines = consumed.splitlines()
    for line in lines:
        try:
            entry = AccountClerkJournalEntry.model_validate_json(line)
        except Exception as exc:
            raise ValueError("malformed complete Clerk journal record") from exc
        if entry.seq <= last_seq:
            raise ValueError("non-monotonic Clerk journal sequence after projection cursor")
        last_seq = entry.seq
        event = event_from_journal(entry)
        if event is not None:
            # A callback can be journaled before its acknowledgement and in a
            # different replay window. Seed the same opaque transaction ID so
            # the later acknowledgement enriches it instead of regressing it.
            transactions.append(row_from_event(account_id, entry, event))
            events.append(event)
    return ClerkTransactionBatch(
        account_id=account_id,
        journal_path=str(journal_path),
        next_byte_offset=offset + len(consumed),
        next_journal_seq=last_seq,
        transactions=transactions,
        events=events,
        source_record_count=len(lines),
        has_more_complete_records=has_more,
        journal_generation=_journal_generation(journal_path) or "",
    )


def read_appended_alpaca_journal(
    *, account_id: str, journal_path: Path, cursor: ClerkJournalCursor | None
) -> ClerkTransactionBatch | None:
    """Tail complete Alpaca Clerk records from their own durable JSONL ledger.

    Alpaca journal lines deliberately have no IBKR-style callback sequence.  The
    projection uses their append ordinal only for keyset ordering and cursor
    progress; the broker's opaque order/event identities stay in the receipt.
    """

    offset = cursor.last_byte_offset if cursor is not None else 0
    last_seq = cursor.last_journal_seq if cursor is not None else 0
    bounded = _read_bounded_complete_tail(
        journal_path=journal_path,
        offset=offset,
        broker="Alpaca",
        max_read_bytes=MAX_PROJECTION_READ_BYTES,
    )
    if bounded is None:
        return None
    consumed, has_more = bounded
    transactions: list[ClerkTransactionRow] = []
    events: list[ClerkTransactionEventRow] = []
    lines = consumed.splitlines()
    for line in lines:
        try:
            entry = OrderJournalEntry.model_validate_json(line)
        except Exception as exc:
            raise ValueError("malformed complete Alpaca Clerk journal record") from exc
        if entry.account_id != account_id:
            raise ValueError("Alpaca Clerk journal account does not match projection account")
        last_seq += 1
        if entry.kind is ClerkEntryKind.SUBMIT_ACKED:
            transaction, event = _row_from_alpaca_ack(account_id, last_seq, entry)
            transactions.append(transaction)
            events.append(event)
        else:
            event = _event_from_alpaca_journal(account_id, last_seq, entry)
            if event is not None:
                events.append(event)
    return ClerkTransactionBatch(
        account_id=account_id,
        journal_path=str(journal_path),
        next_byte_offset=offset + len(consumed),
        next_journal_seq=last_seq,
        transactions=transactions,
        events=events,
        source_record_count=len(lines),
        has_more_complete_records=has_more,
        journal_generation=_journal_generation(journal_path) or "",
    )


async def tail_account_journal(
    *, artifacts_root: Path, account_id: str, store: ClerkTransactionProjectionStore | None = None,
    updated_at_ms: int | None = None,
    allow_rebuilding: bool = False,
) -> int:
    """Project one bounded append-only journal tail. No broker or full scan occurs."""

    resolved_store = store or _default_store()
    path = clerk_journal_path(artifacts_root, account_id)
    cursor = await resolved_store.read_cursor(account_id, str(path))
    generation = _journal_generation(path)
    if cursor is not None and cursor.journal_generation != generation:
        # A repaired journal starts a fresh sequence. Its file identity keeps
        # that recovery from being interpreted as a cursor regression.
        cursor = None
    try:
        batch = read_appended_clerk_journal(account_id=account_id, journal_path=path, cursor=cursor)
    except (ClerkTransactionProjectionCorrupt, ValueError) as exc:
        await resolved_store.set_feed_status(
            account_id,
            "corrupt",
            "Projection corrupt",
            "Projection stopped; canonical Clerk acknowledgement evidence is retained.",
            updated_at_ms=updated_at_ms or _now_ms(),
            high_water_journal_seq=cursor.last_journal_seq if cursor else None,
        )
        raise ClerkTransactionProjectionCorrupt(str(exc)) from exc
    if batch is None:
        return 0
    persisted = await resolved_store.persist_batch(
        batch, updated_at_ms=updated_at_ms or _now_ms(), allow_rebuilding=allow_rebuilding
    )
    if not allow_rebuilding:
        if batch.has_more_complete_records:
            await resolved_store.set_feed_status(
                account_id,
                "reconnecting",
                "Catch-up in progress",
                "Projection remains bounded while durable Clerk records are replayed.",
                updated_at_ms=updated_at_ms or _now_ms(),
                high_water_journal_seq=batch.next_journal_seq,
                lag_records=1,
                lag_is_lower_bound=True,
            )
        else:
            await resolved_store.set_feed_status(
                account_id,
                "live",
                "Live",
                "Durable Clerk callback projection is current.",
                updated_at_ms=updated_at_ms or _now_ms(),
                high_water_journal_seq=batch.next_journal_seq,
                lag_records=0,
            )
    return persisted


async def tail_alpaca_account_journal(
    *, artifacts_root: Path, account_id: str, store: ClerkTransactionProjectionStore | None = None,
    updated_at_ms: int | None = None,
    allow_rebuilding: bool = False,
) -> int:
    """Project one bounded Alpaca ledger tail; it performs no broker I/O."""

    resolved_store = store or _default_store()
    path = alpaca_clerk_journal_path(artifacts_root, account_id)
    cursor = await resolved_store.read_cursor(account_id, str(path))
    generation = _journal_generation(path)
    if cursor is not None and cursor.journal_generation != generation:
        cursor = None
    try:
        batch = read_appended_alpaca_journal(account_id=account_id, journal_path=path, cursor=cursor)
    except (ClerkTransactionProjectionCorrupt, ValueError) as exc:
        await resolved_store.set_feed_status(
            account_id,
            "corrupt",
            "Projection corrupt",
            "Projection stopped; canonical Clerk acknowledgement evidence is retained.",
            updated_at_ms=updated_at_ms or _now_ms(),
            high_water_journal_seq=cursor.last_journal_seq if cursor else None,
        )
        raise ClerkTransactionProjectionCorrupt(str(exc)) from exc
    if batch is None:
        return 0
    persisted = await resolved_store.persist_batch(
        batch, updated_at_ms=updated_at_ms or _now_ms(), allow_rebuilding=allow_rebuilding
    )
    if not allow_rebuilding:
        if batch.has_more_complete_records:
            await resolved_store.set_feed_status(
                account_id,
                "reconnecting",
                "Catch-up in progress",
                "Projection remains bounded while durable Alpaca Clerk records are replayed.",
                updated_at_ms=updated_at_ms or _now_ms(),
                high_water_journal_seq=batch.next_journal_seq,
                lag_records=1,
                lag_is_lower_bound=True,
            )
        else:
            await resolved_store.set_feed_status(
                account_id,
                "live",
                "Live",
                "Durable Alpaca Clerk projection is current.",
                updated_at_ms=updated_at_ms or _now_ms(),
                high_water_journal_seq=batch.next_journal_seq,
                lag_records=0,
            )
    return persisted


async def recover_account_transaction_feed(
    *, artifacts_root: Path, account_id: str, store: ClerkTransactionProjectionStore,
    updated_at_ms: int | None = None,
) -> int:
    """Drain bounded cursor lag before declaring the Clerk transaction feed live."""

    now = updated_at_ms or _now_ms()
    feed_state, *_ = await store.feed_status(account_id)
    if feed_state == "corrupt":
        # Corruption is an operator-visible safety receipt. Automatic recovery
        # must not clear it merely because the next tail happens to parse; an
        # explicit rebuild is the only transition that can reset that state.
        return 0
    await store.set_feed_status(
        account_id,
        "reconnecting",
        "Reconnecting",
        "Replaying durable Clerk callbacks.",
        updated_at_ms=now,
    )
    projected = 0
    path = clerk_journal_path(artifacts_root, account_id)
    try:
        for _ in range(MAX_RECOVERY_BATCHES):
            count = await tail_account_journal(
                artifacts_root=artifacts_root,
                account_id=account_id,
                store=store,
                updated_at_ms=now,
            )
            projected += count
            cursor = await store.read_cursor(account_id, str(path))
            if not _has_complete_appended_record(
                journal_path=path,
                offset=cursor.last_byte_offset if cursor else 0,
                broker="IBKR",
            ):
                await store.set_feed_status(
                    account_id,
                    "live",
                    "Live",
                    "Durable Clerk callback projection is current.",
                    updated_at_ms=now,
                    high_water_journal_seq=cursor.last_journal_seq if cursor else None,
                    lag_records=0,
                )
                return projected
        else:
            cursor = await store.read_cursor(account_id, str(path))
            await store.set_feed_status(
                account_id,
                "reconnecting",
                "Catch-up in progress",
                "Replay remains bounded; canonical Clerk acknowledgement evidence is retained.",
                updated_at_ms=now,
                high_water_journal_seq=cursor.last_journal_seq if cursor else None,
                lag_records=1,
                lag_is_lower_bound=True,
            )
            return projected
    except (ClerkTransactionProjectionCorrupt, ValueError) as exc:
        await store.set_feed_status(
            account_id,
            "corrupt",
            "Projection corrupt",
            "Projection stopped; canonical Clerk acknowledgement evidence is retained.",
            updated_at_ms=now,
        )
        raise ClerkTransactionProjectionCorrupt(str(exc)) from exc
    except Exception:
        await _set_offline_unless_corrupt(store, account_id=account_id, updated_at_ms=now)
        raise


async def recover_alpaca_account_transaction_feed(
    *, artifacts_root: Path, account_id: str, store: ClerkTransactionProjectionStore,
    updated_at_ms: int | None = None,
) -> int:
    """Drain the bounded Alpaca ledger backlog before declaring it current."""

    now = updated_at_ms or _now_ms()
    feed_state, *_ = await store.feed_status(account_id)
    if feed_state == "corrupt":
        # Keep the corruption receipt until the operator explicitly rebuilds
        # the projection from canonical evidence.
        return 0
    await store.set_feed_status(
        account_id,
        "reconnecting",
        "Reconnecting",
        "Replaying durable Alpaca Clerk records.",
        updated_at_ms=now,
    )
    projected = 0
    path = alpaca_clerk_journal_path(artifacts_root, account_id)
    try:
        for _ in range(MAX_RECOVERY_BATCHES):
            projected += await tail_alpaca_account_journal(
                artifacts_root=artifacts_root,
                account_id=account_id,
                store=store,
                updated_at_ms=now,
            )
            cursor = await store.read_cursor(account_id, str(path))
            if not _has_complete_appended_record(
                journal_path=path,
                offset=cursor.last_byte_offset if cursor else 0,
                broker="Alpaca",
            ):
                await store.set_feed_status(
                    account_id,
                    "live",
                    "Live",
                    "Durable Alpaca Clerk projection is current.",
                    updated_at_ms=now,
                    high_water_journal_seq=cursor.last_journal_seq if cursor else None,
                    lag_records=0,
                )
                return projected
        cursor = await store.read_cursor(account_id, str(path))
        await store.set_feed_status(
            account_id,
            "reconnecting",
            "Catch-up in progress",
            "Replay remains bounded; canonical Alpaca Clerk records are retained.",
            updated_at_ms=now,
            high_water_journal_seq=cursor.last_journal_seq if cursor else None,
            lag_records=1,
            lag_is_lower_bound=True,
        )
        return projected
    except (ClerkTransactionProjectionCorrupt, ValueError) as exc:
        await store.set_feed_status(
            account_id,
            "corrupt",
            "Projection corrupt",
            "Projection stopped; canonical Alpaca Clerk evidence is retained.",
            updated_at_ms=now,
        )
        raise ClerkTransactionProjectionCorrupt(str(exc)) from exc
    except Exception:
        await _set_offline_unless_corrupt(store, account_id=account_id, updated_at_ms=now)
        raise


async def rebuild_account_transaction_projection(
    *, artifacts_root: Path, account_id: str, store: ClerkTransactionProjectionStore,
    updated_at_ms: int | None = None, reset: bool = True, broker: Literal["ibkr", "alpaca"] = "ibkr",
) -> int:
    """Reset and replay one broker's read model from canonical Clerk JSONL only."""

    now = updated_at_ms or _now_ms()
    path = (
        clerk_journal_path(artifacts_root, account_id)
        if broker == "ibkr"
        else alpaca_clerk_journal_path(artifacts_root, account_id)
    )
    if not path.is_file():
        raise ClerkTransactionProjectionUnavailable(
            "canonical Clerk journal is unavailable; projection reset was not started"
        )
    if reset:
        await store.reset_projection(
            account_id=account_id, journal_path=str(path), broker=broker, updated_at_ms=now
        )
    projected = 0
    try:
        for _ in range(MAX_RECOVERY_BATCHES):
            if broker == "ibkr":
                projected += await tail_account_journal(
                    artifacts_root=artifacts_root,
                    account_id=account_id,
                    store=store,
                    updated_at_ms=now,
                    allow_rebuilding=True,
                )
            else:
                projected += await tail_alpaca_account_journal(
                    artifacts_root=artifacts_root,
                    account_id=account_id,
                    store=store,
                    updated_at_ms=now,
                    allow_rebuilding=True,
                )
            cursor = await store.read_cursor(account_id, str(path))
            if not _has_complete_appended_record(
                journal_path=path,
                offset=cursor.last_byte_offset if cursor else 0,
                broker="IBKR" if broker == "ibkr" else "Alpaca",
            ):
                await store.set_feed_status(
                    account_id,
                    "live",
                    "Live",
                    "Projection rebuilt from canonical Clerk acknowledgement evidence.",
                    updated_at_ms=now,
                    high_water_journal_seq=cursor.last_journal_seq if cursor else None,
                    lag_records=0,
                )
                return projected
        cursor = await store.read_cursor(account_id, str(path))
        await store.set_feed_status(
            account_id,
            "rebuilding",
            "Rebuild in progress",
            "Projection rebuild is bounded and can be resumed; canonical Clerk acknowledgement evidence is retained.",
            updated_at_ms=now,
            high_water_journal_seq=cursor.last_journal_seq if cursor else None,
            lag_records=1,
            lag_is_lower_bound=True,
        )
        return projected
    except (ClerkTransactionProjectionCorrupt, ValueError) as exc:
        await store.set_feed_status(
            account_id,
            "corrupt",
            "Projection rebuild stopped",
            "Rebuild stopped safely; canonical Clerk acknowledgement evidence is retained.",
            updated_at_ms=now,
        )
        raise ClerkTransactionProjectionCorrupt(str(exc)) from exc
    except Exception:
        await _set_offline_unless_corrupt(
            store,
            account_id=account_id,
            updated_at_ms=now,
            rebuilding=True,
        )
        raise


async def _set_offline_unless_corrupt(
    store: ClerkTransactionProjectionStore,
    *,
    account_id: str,
    updated_at_ms: int,
    rebuilding: bool = False,
) -> None:
    """Record a database outage without erasing a prior corruption receipt."""

    feed_state, *_ = await store.feed_status(account_id)
    if feed_state == "corrupt":
        return
    await store.set_feed_status(
        account_id,
        "offline_but_saved",
        "Projection rebuild paused" if rebuilding else "Offline but saved",
        (
            "Rebuild paused; canonical Clerk acknowledgement evidence is retained."
            if rebuilding
            else "Projection is offline; canonical Clerk evidence is saved."
        ),
        updated_at_ms=updated_at_ms,
    )


async def project_account_journal_best_effort(*, artifacts_root: Path, account_id: str) -> None:
    """Refresh the rebuildable projection without delaying a Clerk acknowledgement."""

    if not settings.CLERK_TRANSACTION_PROJECTION_ENABLED:
        return
    try:
        await recover_account_transaction_feed(
            artifacts_root=artifacts_root,
            account_id=account_id,
            store=_default_store(),
        )
    except Exception:
        logger.exception(
            "Clerk transaction projection failed after durable acknowledgement",
            extra={"account_id": account_id},
        )


async def project_alpaca_journal_best_effort(*, artifacts_root: Path, account_id: str) -> None:
    """Refresh the Alpaca projection after durable evidence; never call Alpaca here."""

    if not settings.CLERK_TRANSACTION_PROJECTION_ENABLED:
        return
    try:
        await recover_alpaca_account_transaction_feed(
            artifacts_root=artifacts_root,
            account_id=account_id,
            store=_default_store(),
        )
    except Exception:
        logger.exception(
            "Alpaca transaction projection failed after durable journal append",
            extra={"account_id": account_id, "broker": "alpaca"},
        )


def _encode_cursor(value: tuple[int, int, str]) -> str:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return "ctxhp1." + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


_POSTGRES_BIGINT_MAX = 2**63 - 1


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
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or not 0 <= timestamp <= _POSTGRES_BIGINT_MAX
        or isinstance(seq, bool)
        or not isinstance(seq, int)
        or not 1 <= seq <= _POSTGRES_BIGINT_MAX
        or not isinstance(transaction_id, str)
    ):
        raise ValueError("invalid transaction history cursor")
    return timestamp, seq, transaction_id


async def transaction_history(
    *,
    account_id: str,
    limit: int,
    cursor: str | None,
    origin: TransactionOrigin | None = None,
    lifecycle_state: str | None = None,
    strategy_instance_id: str | None = None,
    run_id: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
    store: ClerkTransactionProjectionStore | None = None,
) -> ClerkTransactionHistoryResponse:
    resolved_store = store or _default_store()
    rows, high_water, lag = await resolved_store.history_page(
        account_id=account_id,
        limit=limit,
        after=_decode_cursor(cursor),
        origin=origin,
        lifecycle_state=lifecycle_state,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    feed_state, feed_headline, feed_detail, status_high_water, status_lag, lag_is_lower_bound = (
        await resolved_store.feed_status(account_id)
    )
    next_cursor = (
        _encode_cursor((rows[-1].recorded_at_ms, rows[-1].journal_seq, rows[-1].transaction_id))
        if len(rows) == limit
        else None
    )
    return ClerkTransactionHistoryResponse(
        projection_available=True,
        canonical_fallback_required=False,
        feed_state=feed_state,
        feed_headline=feed_headline,
        feed_detail=feed_detail,
        high_water_journal_seq=(
            status_high_water
            if status_high_water is not None or feed_state in {"rebuilding", "reconnecting"}
            else high_water
        ),
        lag_records=(
            status_lag if status_lag is not None or feed_state in {"rebuilding", "reconnecting"} else lag
        ),
        lag_is_lower_bound=lag_is_lower_bound,
        custody_summary=custody_window_summary(rows),
        rows=rows,
        next_cursor=next_cursor,
    )


def custody_window_summary(
    rows: list[ClerkTransactionSummaryRow],
) -> ClerkCustodyWindowSummary:
    """Fold only backend-projected lifecycle vocabulary into bounded counts.

    This intentionally leaves unsupported or ambiguous vocabulary uncertain
    rather than guessing a more advanced custody stage in the browser.
    """

    counts = {"a0": 0, "a1": 0, "a2": 0, "a3": 0, "uncertain": 0}
    for row in rows:
        stage = _CUSTODY_STAGE_BY_LIFECYCLE.get(row.lifecycle_state, "uncertain")
        counts[stage] += 1
    return ClerkCustodyWindowSummary(
        record_count=len(rows),
        a0_custody_accepted_count=counts["a0"],
        a1_broker_write_started_count=counts["a1"],
        a2_broker_known_count=counts["a2"],
        a3_economic_terminal_count=counts["a3"],
        uncertain_count=counts["uncertain"],
    )


async def transaction_detail(
    *, account_id: str, transaction_id: str, store: ClerkTransactionProjectionStore | None = None
) -> ClerkTransactionRow | None:
    """Read one operator-selected receipt from the indexed projection only."""

    resolved_store = store or _default_store()
    row = await resolved_store.transaction_detail(
        account_id=account_id, transaction_id=transaction_id
    )
    if row is None:
        return None
    timeline = custody_timeline_from_transaction(row, now_ms=now_ms_utc())
    return row.model_copy(update={"custody_timeline": timeline})


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def __getattr__(name: str) -> object:
    """Retain the original store import path while keeping orchestration focused."""

    if name == "PostgresClerkTransactionProjectionStore":
        from app.services.clerk_transaction_projection_store import PostgresClerkTransactionProjectionStore

        return PostgresClerkTransactionProjectionStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ClerkJournalCursor",
    "ClerkTransactionBatch",
    "ClerkTransactionProjectionCorrupt",
    "ClerkTransactionProjectionStore",
    "ClerkTransactionProjectionUnavailable",
    "PostgresClerkTransactionProjectionStore",
    "alpaca_clerk_journal_path",
    "clerk_journal_path",
    "custody_window_summary",
    "fold_lifecycle_state",
    "project_account_journal_best_effort",
    "project_alpaca_journal_best_effort",
    "read_appended_alpaca_journal",
    "read_appended_clerk_journal",
    "rebuild_account_transaction_projection",
    "recover_account_transaction_feed",
    "recover_alpaca_account_transaction_feed",
    "tail_account_journal",
    "tail_alpaca_account_journal",
    "transaction_detail",
    "transaction_history",
]
