"""Durable, bounded Alpaca account-activity recovery for the Clerk."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.broker.alpaca.clerk.journal import OrderJournal
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerActivity

type Clock = Callable[[], int]
type EnsureJournal = Callable[[], Awaitable[tuple[str, OrderJournal]]]


class AlpacaActivityRecovery:
    """Own the journal-derived activity cursor and idempotent recovery writes."""

    def __init__(
        self,
        *,
        intake_lock: asyncio.Lock,
        ensure_journal: EnsureJournal,
        clock: Clock,
    ) -> None:
        self._intake_lock = intake_lock
        self._ensure_journal = ensure_journal
        self._clock = clock

    async def cursor_ms(self) -> int | None:
        """Return the durable high-water mark, never an in-memory cursor."""
        async with self._intake_lock:
            _, journal = await self._ensure_journal()
            return max(
                (
                    entry.activity.occurred_at_ms
                    for entry in journal.read_entries()
                    if entry.kind is ClerkEntryKind.ACTIVITY_RECOVERY
                    and entry.activity is not None
                    and entry.activity.occurred_at_ms is not None
                ),
                default=None,
            )

    async def record(self, *, activity: BrokerActivity, window_limit: int) -> bool:
        """Append each recovered activity once, including unowned cursor evidence."""
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            entries = journal.read_entries()
            if any(
                entry.kind is ClerkEntryKind.ACTIVITY_RECOVERY
                and entry.activity is not None
                and entry.activity.activity_id == activity.activity_id
                for entry in entries
            ):
                return False
            owner = self._owner_for_order_id(activity.native_order_id, entries)
            await journal.append_async(
                OrderJournalEntry(
                    kind=ClerkEntryKind.ACTIVITY_RECOVERY,
                    account_id=account_id,
                    operator=owner.operator if owner is not None else "",
                    intent_id=owner.intent_id if owner is not None else "",
                    order_ref=owner.order_ref if owner is not None else "",
                    client_order_id=owner.client_order_id if owner is not None else "",
                    leg=owner.leg if owner is not None else None,
                    broker_order_id=activity.native_order_id,
                    owned=owner is not None,
                    recorded_at_ms=self._clock(),
                    activity=activity,
                    recovery_source="account_activities_cursor_window",
                    recovery_window_limit=window_limit,
                )
            )
            return True

    @staticmethod
    def _owner_for_order_id(
        broker_order_id: str | None, entries: list[OrderJournalEntry]
    ) -> OrderJournalEntry | None:
        if not broker_order_id:
            return None
        for entry in reversed(entries):
            if (
                entry.order is not None
                and entry.order.order_id == broker_order_id
                and entry.order_ref
            ):
                return entry
        return None
