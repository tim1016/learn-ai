"""Durable, bounded Alpaca account-activity recovery for the Clerk."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.broker.alpaca.clerk import derive
from app.broker.alpaca.clerk.journal import OrderJournal
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerActivity
from app.utils.timestamps import Clock

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
            owner = derive.order_owner(entries, activity.native_order_id)
            await journal.append_async(
                OrderJournalEntry.attributed_from(
                    owner,
                    kind=ClerkEntryKind.ACTIVITY_RECOVERY,
                    account_id=account_id,
                    client_order_id=owner.client_order_id if owner is not None else "",
                    broker_order_id=activity.native_order_id,
                    owned=owner is not None,
                    recorded_at_ms=self._clock(),
                    activity=activity,
                    recovery_source="account_activities_cursor_window",
                    recovery_window_limit=window_limit,
                )
            )
            return True
