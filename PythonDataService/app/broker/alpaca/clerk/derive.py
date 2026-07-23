"""Pure ledger-derivation helpers for the Alpaca Clerk (phase 2, S6).

The Clerk's durable state is the append-only journal — namespaces, terminals,
unresolved intents, the exposure hold, and the latest reconciliation verdict are
all *derived* from the ledger, never held as an in-memory flag, so they survive a
restart. These functions are the S6 derivations: pure functions over a pre-read
``list[OrderJournalEntry]``, with no I/O and no Clerk state, so they are trivially
testable and share one place with the ``clerk.py`` intent/terminal scanners.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.models import (
    ClerkEntryKind,
    HoldState,
    OrderJournalEntry,
    ReconciliationSummary,
)
from app.broker.contract.models import BrokerOrder, BrokerPosition


def hold_state(entries: list[OrderJournalEntry]) -> HoldState:
    """Derive the exposure-hold state from the ledger.

    A ``HOLD_SET`` with no later ``HOLD_CLEARED`` is active. Last write wins: the
    most recent of the two kinds decides, so the hold survives a restart with no
    in-memory flag.
    """
    active = False
    reason_code: str | None = None
    reason: str | None = None
    since_ms: int | None = None
    for entry in entries:
        if entry.kind is ClerkEntryKind.HOLD_SET:
            active = True
            reason_code = entry.reason_code
            reason = entry.reason
            since_ms = entry.recorded_at_ms
        elif entry.kind is ClerkEntryKind.HOLD_CLEARED:
            active = False
            reason_code = None
            reason = None
            since_ms = None
    return HoldState(
        active=active, reason_code=reason_code, reason=reason, since_ms=since_ms
    )


def latest_reconciliation(
    entries: list[OrderJournalEntry],
) -> ReconciliationSummary | None:
    """The most recent RECONCILIATION verdict, or ``None`` if never swept."""
    latest: OrderJournalEntry | None = None
    for entry in entries:
        if entry.kind is ClerkEntryKind.RECONCILIATION and entry.verdict:
            latest = entry
    if latest is None or latest.verdict is None:
        return None
    return ReconciliationSummary(
        verdict=latest.verdict, recorded_at_ms=latest.recorded_at_ms
    )


def has_missing_intent(
    entries: list[OrderJournalEntry],
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
) -> bool:
    """True when the broker reflects owned exposure with no recorded intent.

    Every non-foreign order the sweep saw carries a ``client_order_id`` that is
    one of our namespaces (foreign orders are handled before this check). An owned
    order for which the ledger has no ``intent_recorded`` line means the broker
    knows about an order we never recorded intent for — drift. A position while
    the ledger has never recorded a single owned order is the same drift signal
    from the position side. Observational either way.
    """
    recorded_refs = {
        entry.order_ref
        for entry in entries
        if entry.kind is ClerkEntryKind.INTENT_RECORDED and entry.order_ref
    }
    for order in orders:
        coid = order.client_order_id
        if coid and coid not in recorded_refs:
            return True
    return bool(positions and not recorded_refs)
