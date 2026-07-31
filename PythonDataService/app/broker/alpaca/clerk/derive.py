"""Pure ledger-derivation helpers for the Alpaca Clerk (phase 2, S6).

The Clerk's durable state is the append-only journal — namespaces, terminals,
unresolved intents, the exposure hold, and the latest reconciliation verdict are
all *derived* from the ledger, never held as an in-memory flag, so they survive a
restart. These functions are the S6 derivations: pure functions over a pre-read
``list[OrderJournalEntry]``, with no I/O and no Clerk state, so they are trivially
testable and share one place with the ``clerk.py`` intent/terminal scanners.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.exposure import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
    project_expected_account_exposure,
    signed_broker_position_quantity,
)
from app.broker.alpaca.clerk.models import (
    UNEXPLAINED_ORDER_HOLD_CODE,
    AccountFreezeState,
    ChannelHealth,
    ClerkEntryKind,
    ClerkStatus,
    HoldState,
    OrderJournalEntry,
    OrderLegError,
    OrderLegResult,
    ReconciliationSummary,
)
from app.broker.contract.models import BrokerOrder, BrokerPosition

# Terminal order statuses: settled exposure is represented by the broker
# positions snapshot, not by further order-state changes. ``reconcile``
# re-exports this for the sweep's working-order filter.
RECONCILIATION_TERMINAL_ORDER_STATUSES = ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES


def build_status(
    entries: list[OrderJournalEntry],
    *,
    broker_id: str,
    account_id: str,
    outstanding_intents: int,
    observed_at_ms: int,
    channel_healths: list[ChannelHealth] | None = None,
) -> ClerkStatus:
    """Shape one ``ClerkStatus`` from a pre-read ledger (the single builder).

    ``outstanding_intents`` is supplied by the Clerk (it owns the intent-scan);
    hold and latest verdict are derived here so status is a pure read of the
    durable ledger and survives a restart.
    """
    return ClerkStatus(
        broker=broker_id,
        account_id=account_id,
        hold=hold_state(entries),
        freeze=account_freeze_state(entries),
        latest_reconciliation=latest_reconciliation(entries),
        outstanding_intents=outstanding_intents,
        observed_at_ms=observed_at_ms,
        channel_healths=channel_healths,
    )


def account_freeze_state(entries: list[OrderJournalEntry]) -> AccountFreezeState:
    """Project the latest durable reconciliation into exactly two categories.

    Broker state that cannot be mapped to exact Clerk custody is
    ``ACCOUNT_STATE_UNATTRIBUTABLE``. A failed fresh broker observation is
    ``ACCOUNT_STATE_UNPROVABLE``. A later clean sweep clears either projection;
    instance uncertainty and channel health never enter this fold.
    """
    summary = latest_reconciliation(entries)
    if summary is None or summary.verdict == "clean":
        return AccountFreezeState()
    if summary.verdict in {"unexplained_order", "missing_intent"}:
        return AccountFreezeState(
            active=True,
            category="ACCOUNT_STATE_UNATTRIBUTABLE",
            explanation=(
                "Fresh broker state exists but cannot be mapped to exact "
                "Clerk-authored custody."
            ),
            next_step=(
                "Run Clerk recovery and reconcile until every broker slice has "
                "an exact durable attribution."
            ),
            observed_at_ms=summary.recorded_at_ms,
        )
    return AccountFreezeState(
        active=True,
        category="ACCOUNT_STATE_UNPROVABLE",
        explanation=(
            "The Clerk could not establish current order and exposure truth "
            "from a fresh broker observation."
        ),
        next_step=(
            "Restore broker observability, then run reconciliation before "
            "starting or resuming any bot."
        ),
        observed_at_ms=summary.recorded_at_ms,
    )


def hold_state(entries: list[OrderJournalEntry]) -> HoldState:
    """Derive the exposure-hold state from the ledger.

    A ``HOLD_SET`` with no later ``HOLD_CLEARED`` is active. An
    ``UNEXPLAINED_ORDER`` after the latest clear is also an active hold: this
    fail-closed derivation covers a crash or append failure between recording the
    foreign order and the separate ``HOLD_SET`` receipt. Last write wins, so an
    operator's later clear remains effective until another observation arrives.
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
        elif entry.kind is ClerkEntryKind.UNEXPLAINED_ORDER:
            active = True
            reason_code = UNEXPLAINED_ORDER_HOLD_CODE
            reason = (
                "An order this account did not submit was recorded in the "
                "Alpaca clerk journal."
            )
            since_ms = entry.recorded_at_ms
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


def unexplained_order_ids(entries: list[OrderJournalEntry]) -> set[str]:
    """Broker order ids already journaled as ``UNEXPLAINED_ORDER``.

    The dedup key for the reconciliation sweep: a foreign order the sweep has
    already recorded must not be re-journaled (nor re-counted) on every later
    pass, or a single persistent foreign order would grow the ledger without
    bound while the account is held.
    """
    return {
        entry.broker_order_id
        for entry in entries
        if entry.kind is ClerkEntryKind.UNEXPLAINED_ORDER and entry.broker_order_id
    }


def order_owner(
    entries: list[OrderJournalEntry],
    broker_order_id: str | None,
    *,
    kind: ClerkEntryKind | None = None,
) -> OrderJournalEntry | None:
    """Return the latest journal owner of a broker order id.

    ``kind`` narrows callers that require a particular ownership receipt, such
    as callback attribution's ``SUBMIT_ACKED`` binding. Recovery callers omit
    it because any later order snapshot preserves the same minted owner.
    """
    if not broker_order_id:
        return None
    for entry in reversed(entries):
        if (
            (kind is None or entry.kind is kind)
            and entry.order is not None
            and entry.order.order_id == broker_order_id
            and entry.order_ref
        ):
            return entry
    return None


def order_owner_by_ref(
    entries: list[OrderJournalEntry],
    order_ref: str,
) -> OrderJournalEntry | None:
    """Return the latest submit-side owner for a minted order reference."""
    for entry in reversed(entries):
        if (
            entry.kind
            in (ClerkEntryKind.SUBMIT_ACKED, ClerkEntryKind.INTENT_RECORDED)
            and entry.order_ref == order_ref
        ):
            return entry
    return None


def reconstruct_terminal(entry: OrderJournalEntry) -> OrderLegResult:
    """Rebuild the result represented by one terminal submit-side ledger line."""
    if entry.kind is ClerkEntryKind.SUBMIT_ACKED:
        return OrderLegResult(
            status="acked",
            order_ref=entry.order_ref,
            intent_id=entry.intent_id,
            order=entry.order,
        )
    return OrderLegResult(
        status="failed",
        order_ref=entry.order_ref,
        intent_id=entry.intent_id,
        error=OrderLegError(
            message=entry.error_message or "The order did not reach the broker.",
            why=entry.error_detail,
        ),
    )


def terminal_outcome(
    entries: list[OrderJournalEntry], order_ref: str
) -> OrderLegResult | None:
    """Last terminal result for an order ref, if any (journal last-write-wins)."""
    terminal: OrderJournalEntry | None = None
    for entry in entries:
        if (
            entry.kind in (ClerkEntryKind.SUBMIT_ACKED, ClerkEntryKind.SUBMIT_FAILED)
            and entry.order_ref == order_ref
        ):
            terminal = entry
    return None if terminal is None else reconstruct_terminal(terminal)


def terminal_map(entries: list[OrderJournalEntry]) -> dict[str, OrderLegResult]:
    """Order ref → terminal result from one ledger scan (last-write-wins)."""
    latest: dict[str, OrderJournalEntry] = {}
    for entry in entries:
        if (
            entry.order_ref
            and entry.kind in (ClerkEntryKind.SUBMIT_ACKED, ClerkEntryKind.SUBMIT_FAILED)
        ):
            latest[entry.order_ref] = entry
    return {order_ref: reconstruct_terminal(entry) for order_ref, entry in latest.items()}


def uncertain_timestamp_map(entries: list[OrderJournalEntry]) -> dict[str, int]:
    """Most recent response-lost-submit timestamp per durable order ref."""
    return {
        entry.order_ref: entry.recorded_at_ms
        for entry in entries
        if entry.order_ref and entry.kind is ClerkEntryKind.SUBMIT_UNCERTAIN
    }


def unresolved_intents(entries: list[OrderJournalEntry]) -> list[OrderJournalEntry]:
    """Owning intent lines whose latest submit-side state is not terminal."""
    origin: dict[str, OrderJournalEntry] = {}
    terminal_refs: set[str] = set()
    order: list[str] = []
    for entry in entries:
        if not entry.order_ref:
            continue
        if entry.kind is ClerkEntryKind.INTENT_RECORDED:
            if entry.order_ref not in origin:
                origin[entry.order_ref] = entry
                order.append(entry.order_ref)
        elif entry.kind in (ClerkEntryKind.SUBMIT_ACKED, ClerkEntryKind.SUBMIT_FAILED):
            terminal_refs.add(entry.order_ref)
    return [
        origin[order_ref]
        for order_ref in order
        if order_ref not in terminal_refs and origin[order_ref].leg is not None
    ]


def has_missing_intent(
    entries: list[OrderJournalEntry],
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
) -> bool:
    """True when the broker reflects owned exposure with no recorded intent.

    Every non-foreign order the sweep saw carries a ``client_order_id`` that is
    one of our namespaces (foreign orders are handled before this check). An owned
    order for which the ledger has no ``intent_recorded`` line means the broker
    knows about an order we never recorded intent for — drift. Position-side drift
    compares each live symbol/quantity to the latest journaled order state; an old
    unrelated intent can therefore never mask manual exposure. Observational
    either way.
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
    # Only settled (terminal) snapshots contribute to the expected tally: a
    # working snapshot's filled_quantity goes stale the instant the broker
    # fills it, and the authoritative fill ORDER_EVENT may not have been
    # journaled yet when the sweep reads (observed live 2026-07-30: verdict
    # fired ~7s before the fill event landed). A symbol with such an in-flight
    # order is skipped for this pass — the next sweep, after the fill event
    # lands, decides. A position with no journal presence still flags
    # (foreign exposure must not be masked by this suppression).
    expected_quantity_by_symbol = project_expected_account_exposure(entries)
    symbols_with_inflight_order: set[str] = set()
    for entry in entries:
        order = entry.order
        if order is not None and order.status.lower() not in RECONCILIATION_TERMINAL_ORDER_STATUSES:
            symbols_with_inflight_order.add(order.symbol.upper())

    actual_quantity_by_symbol: dict[str, float] = {}
    for position in positions:
        symbol = position.symbol.upper()
        if symbol in symbols_with_inflight_order:
            continue
        actual_quantity = signed_broker_position_quantity(position)
        actual_quantity_by_symbol[symbol] = (
            actual_quantity_by_symbol.get(symbol, 0.0) + actual_quantity
        )

    for symbol in set(expected_quantity_by_symbol) | set(actual_quantity_by_symbol):
        if symbol in symbols_with_inflight_order:
            continue
        expected_quantity = expected_quantity_by_symbol.get(symbol, 0.0)
        actual_quantity = actual_quantity_by_symbol.get(symbol, 0.0)
        if abs(actual_quantity - expected_quantity) > 1e-9:
            return True
    return False
