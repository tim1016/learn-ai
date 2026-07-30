"""Six-station transaction-rail derivation (spec §7.1).

Pure functions that turn one bot's decision receipts + the account order
journal into the six-station rail for a *selected transaction*. The rail is a
property of a transaction (one order intent), not the whole bot (§7.1).

Station derivation reads the durable evidence and never guesses:

    SIGNAL      — the latest decision receipt from the bot's decision journal.
    INTENT      — an ``intent_recorded`` line for the transaction's order_ref.
    SUBMIT_GATE — did the submit clear the holds/health gate, or was it held?
    BROKER_ACK  — ``submit_acked`` (satisfied) / ``submit_failed`` (blocked) /
                  ``submit_uncertain`` (unknown_stale).
    FILL        — an ``order_event`` fill for the order_ref.
    RECONCILED  — the latest account reconciliation verdict.

The five station states come from the vocabulary (satisfied / waiting /
blocked / unknown_stale / not_applicable). Freshness (``unknown_stale``) is a
threshold on the evidence timestamp against ``now_ms``.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.decision_journal import DecisionReceipt
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.v2panel.vocabulary import STATION_IDS, StationId, StationState, copy_for
from app.engine.live.order_identity import build_bot_order_namespace, parse_order_ref
from app.schemas.broker_v2_panel import StationView

# Evidence older than this is rendered ``unknown_stale`` (§7). One trading day
# in ms — a decision or sweep older than the current session cannot be trusted
# to describe the live pipeline.
STALE_THRESHOLD_MS = 24 * 60 * 60 * 1000


def transaction_refs_for_bot(sid: str, entries: list[OrderJournalEntry]) -> list[str]:
    """Return the bot's order_refs in journal order (most recent last).

    An order_ref names one transaction (one intent). The panel selects the most
    recent by default; the journal tail can select any (§7.1).
    """
    target_namespace = build_bot_order_namespace(sid)
    refs: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        ref = entry.order_ref
        if not ref or ref in seen:
            continue
        try:
            namespace, _ = parse_order_ref(ref)
        except ValueError:
            continue
        if namespace != target_namespace:
            continue
        seen.add(ref)
        refs.append(ref)
    return refs


def _entries_for_ref(ref: str, entries: list[OrderJournalEntry]) -> list[OrderJournalEntry]:
    return [entry for entry in entries if entry.order_ref == ref]


def _station(
    station_id: StationId,
    state: StationState,
    receipt: str,
    *,
    evidence_at_ms: int | None = None,
) -> StationView:
    copy = copy_for(station_id)
    state_copy = copy_for(state)
    return StationView(
        station_id=station_id,
        label=copy.label,
        state=state,
        state_label=state_copy.label,
        receipt=receipt,
        evidence_at_ms=evidence_at_ms,
        blocker=None,
    )


def _freshness(state: StationState, evidence_at_ms: int | None, now_ms: int) -> StationState:
    """Downgrade a satisfied station to ``unknown_stale`` when evidence is old."""
    if (
        state == "satisfied"
        and evidence_at_ms is not None
        and now_ms - evidence_at_ms > STALE_THRESHOLD_MS
    ):
        return "unknown_stale"
    return state


def _signal_station(latest_decision: DecisionReceipt | None, now_ms: int) -> StationView:
    if latest_decision is None:
        return _station("SIGNAL", "waiting", "No bar has been evaluated yet.")
    state = _freshness("satisfied", latest_decision.ts_ms, now_ms)
    receipt = (
        f"Latest decision: {latest_decision.outcome} "
        f"({latest_decision.reason_code}) on bar {latest_decision.bar_ref}."
    )
    return _station("SIGNAL", state, receipt, evidence_at_ms=latest_decision.ts_ms)


def _intent_station(txn_entries: list[OrderJournalEntry]) -> StationView:
    for entry in txn_entries:
        if entry.kind is ClerkEntryKind.INTENT_RECORDED:
            return _station(
                "INTENT",
                "satisfied",
                "Order intent recorded before touching the broker.",
                evidence_at_ms=entry.recorded_at_ms,
            )
    return _station("INTENT", "waiting", "No order intent recorded yet.")


def _submit_gate_station(txn_entries: list[OrderJournalEntry]) -> StationView:
    # If the submit reached the broker (acked/failed/uncertain), the gate was
    # passed. If only an intent exists, the gate is still pending (waiting).
    for entry in txn_entries:
        if entry.kind in (
            ClerkEntryKind.SUBMIT_ACKED,
            ClerkEntryKind.SUBMIT_FAILED,
            ClerkEntryKind.SUBMIT_UNCERTAIN,
        ):
            return _station(
                "SUBMIT_GATE",
                "satisfied",
                "Holds and channel health were clear; submission proceeded.",
                evidence_at_ms=entry.recorded_at_ms,
            )
    return _station("SUBMIT_GATE", "waiting", "Submission has not been attempted.")


def _broker_ack_station(txn_entries: list[OrderJournalEntry]) -> StationView:
    for entry in txn_entries:
        if entry.kind is ClerkEntryKind.SUBMIT_ACKED:
            return _station(
                "BROKER_ACK",
                "satisfied",
                "The broker acknowledged the order.",
                evidence_at_ms=entry.recorded_at_ms,
            )
        if entry.kind is ClerkEntryKind.SUBMIT_FAILED:
            return _station(
                "BROKER_ACK",
                "blocked",
                entry.error_message or "The broker rejected the order.",
                evidence_at_ms=entry.recorded_at_ms,
            )
        if entry.kind is ClerkEntryKind.SUBMIT_UNCERTAIN:
            return _station(
                "BROKER_ACK",
                "unknown_stale",
                "The submit's outcome is unknown; the order may or may not have landed.",
                evidence_at_ms=entry.recorded_at_ms,
            )
    return _station("BROKER_ACK", "waiting", "Awaiting a broker acknowledgement.")


def _fill_station(txn_entries: list[OrderJournalEntry]) -> StationView:
    for entry in txn_entries:
        if (
            entry.kind is ClerkEntryKind.ORDER_EVENT
            and entry.event is not None
            and entry.event.event_type in ("fill", "partial_fill")
        ):
            return _station(
                "FILL",
                "satisfied",
                f"Order {entry.event.event_type} recorded.",
                evidence_at_ms=entry.recorded_at_ms,
            )
    return _station("FILL", "waiting", "No fill has been recorded for this order.")


def _reconciled_station(
    latest_reconciliation: OrderJournalEntry | None, now_ms: int
) -> StationView:
    if latest_reconciliation is None or latest_reconciliation.verdict is None:
        return _station("RECONCILED", "waiting", "No reconciliation sweep has run yet.")
    verdict = latest_reconciliation.verdict
    at_ms = latest_reconciliation.recorded_at_ms
    if verdict == "clean":
        state = _freshness("satisfied", at_ms, now_ms)
        return _station(
            "RECONCILED", state, "The last sweep found the account clean.", evidence_at_ms=at_ms
        )
    if verdict == "stale":
        return _station(
            "RECONCILED",
            "unknown_stale",
            "The last sweep could not reach the broker.",
            evidence_at_ms=at_ms,
        )
    # unexplained_order / missing_intent
    return _station(
        "RECONCILED",
        "blocked",
        f"The last sweep found: {verdict}.",
        evidence_at_ms=at_ms,
    )


def derive_stations(
    *,
    transaction_ref: str | None,
    all_entries: list[OrderJournalEntry],
    latest_decision: DecisionReceipt | None,
    latest_reconciliation: OrderJournalEntry | None,
    now_ms: int,
) -> list[StationView]:
    """Derive the six-station rail for one selected transaction (§7.1).

    ``transaction_ref`` selects the transaction; ``None`` means the bot has no
    order intent yet, so every order-scoped station is ``waiting``. SIGNAL and
    RECONCILED are bot/account-scoped and derived regardless.
    """
    txn_entries = (
        _entries_for_ref(transaction_ref, all_entries) if transaction_ref else []
    )
    builders = {
        "SIGNAL": _signal_station(latest_decision, now_ms),
        "INTENT": _intent_station(txn_entries),
        "SUBMIT_GATE": _submit_gate_station(txn_entries),
        "BROKER_ACK": _broker_ack_station(txn_entries),
        "FILL": _fill_station(txn_entries),
        "RECONCILED": _reconciled_station(latest_reconciliation, now_ms),
    }
    return [builders[station_id] for station_id in STATION_IDS]
