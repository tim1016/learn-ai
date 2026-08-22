"""Six-station transaction-rail derivation (spec §7.1).

Pure functions that turn one bot's decision receipts + the account order
journal into the six-station rail for a *selected transaction*. The rail is a
property of a transaction (one order intent), not the whole bot (§7.1).

Station derivation reads the durable evidence and never guesses:

    SIGNAL      — the decision receipt causally linked to the selected
                  transaction (via order_ref/intent_id), or the bot's own
                  latest decision when no transaction is selected.
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

import logging

from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.alpaca.clerk.sqlite.decision_receipts import DecisionReceipt
from app.broker.v2panel.vocabulary import STATION_IDS, StationId, StationState, copy_for
from app.engine.live.order_identity import build_bot_order_namespace, parse_order_ref
from app.schemas.broker_v2_panel import StationView

logger = logging.getLogger(__name__)

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


def _validate_transaction_ownership(transaction_ref: str, sid: str) -> bool:
    """Return True iff ``transaction_ref`` belongs to bot ``sid``'s namespace.

    Parses the ref via ``parse_order_ref`` and compares its namespace to the
    expected value from ``build_bot_order_namespace``. A mismatch means a
    cross-bot order ref slipped through the caller's filter — the order-scoped
    stations must not render as this bot's evidence.
    """
    try:
        namespace, _ = parse_order_ref(transaction_ref)
    except ValueError:
        return False
    return namespace == build_bot_order_namespace(sid)


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


def _decision_for_transaction(
    transaction_ref: str, decisions: list[DecisionReceipt]
) -> DecisionReceipt | None:
    """The bot's own most recent decision causally linked to ``transaction_ref``.

    Mirrors the exact-key join
    ``decision_receipts_by_transaction`` performs in storage
    (``intent_id = ? OR order_ref = ?``) over the bounded in-memory window —
    never the nearest-time or most-recent decision (PRD Sec 19).
    """
    matches = [
        decision
        for decision in decisions
        if decision.order_ref == transaction_ref or decision.intent_id == transaction_ref
    ]
    return matches[-1] if matches else None


def _signal_station(
    transaction_ref: str | None,
    decisions: list[DecisionReceipt],
    now_ms: int,
) -> StationView:
    """Derive SIGNAL from the decision causally linked to ``transaction_ref``.

    ``transaction_ref is None`` means no transaction is selected — SIGNAL then
    falls back to the bot's own latest decision, which is an honest default,
    not a substitution against an explicit request. When a transaction *is*
    selected, SIGNAL must show that transaction's own decision (or explicit
    absence) rather than an unrelated newer one (issue #1729 AC #6/#7).
    """
    if transaction_ref is None:
        latest_decision = decisions[-1] if decisions else None
        if latest_decision is None:
            return _station("SIGNAL", "waiting", "No bar has been evaluated yet.")
    else:
        latest_decision = _decision_for_transaction(transaction_ref, decisions)
        if latest_decision is None:
            return _station(
                "SIGNAL",
                "not_applicable",
                "No decision in the retained evidence window is linked to "
                f"transaction {transaction_ref!r}.",
            )
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
    latest_reconciliation: OrderJournalEntry | None,
    now_ms: int,
    transaction_submitted_at_ms: int | None = None,
) -> StationView:
    if latest_reconciliation is None or latest_reconciliation.verdict is None:
        return _station("RECONCILED", "waiting", "No reconciliation sweep has run yet.")
    verdict = latest_reconciliation.verdict
    at_ms = latest_reconciliation.recorded_at_ms
    # Causal gate: a sweep that predates the transaction's submission cannot
    # confirm this transaction is reconciled — it only saw the prior state.
    if (
        transaction_submitted_at_ms is not None
        and at_ms <= transaction_submitted_at_ms
    ):
        return _station(
            "RECONCILED",
            "blocked",
            "The last reconciliation sweep predates this transaction's submission; "
            "re-run a sweep.",
            evidence_at_ms=at_ms,
        )
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
    sid: str,
    transaction_ref: str | None,
    all_entries: list[OrderJournalEntry],
    decisions: list[DecisionReceipt],
    latest_reconciliation: OrderJournalEntry | None,
    now_ms: int,
) -> list[StationView]:
    """Derive the six-station rail for one selected transaction (§7.1).

    ``transaction_ref`` selects the transaction; ``None`` means no transaction
    is selected, so every order-scoped station is ``waiting`` and SIGNAL falls
    back to the bot's own latest decision. RECONCILED is account-scoped and
    derived regardless — it carries its own causal gate below.

    ``decisions`` is the bot's own bounded decision-receipt window (ascending
    by ``seq``), used to derive SIGNAL. When a transaction *is* selected,
    SIGNAL must show the decision causally linked to it (via ``order_ref`` /
    ``intent_id``) — never an unrelated, more recent decision (issue #1729
    AC #6/#7; PRD Sec 19 forbids joining by "latest signal, latest
    transaction"). A decision-receipt store call outside this bounded window
    is the caller's responsibility if the deeper stored-key join is needed.

    Ownership validation: if ``transaction_ref`` is set but does not belong to
    ``sid``'s namespace (cross-bot contamination), every transaction-scoped
    station (INTENT, SUBMIT_GATE, BROKER_ACK, FILL, and now SIGNAL) is
    blocked with an explanatory receipt rather than leaking this bot's own
    unrelated evidence into a foreign transaction's rail.

    Causal reconciliation gate: if the selected transaction has a SUBMIT_ACKED
    entry, any reconciliation sweep whose timestamp is <= the submission timestamp
    predates the transaction and must not be treated as confirming evidence.
    """
    ownership_ok = True
    if transaction_ref is not None and not _validate_transaction_ownership(
        transaction_ref, sid
    ):
        logger.warning(
            "Transaction ownership mismatch: ref=%s does not belong to sid=%s",
            transaction_ref,
            sid,
            extra={"transaction_ref": transaction_ref, "sid": sid},
        )
        ownership_ok = False

    if transaction_ref is not None and ownership_ok:
        txn_entries = _entries_for_ref(transaction_ref, all_entries)
    else:
        txn_entries = []

    if ownership_ok:
        intent_station = _intent_station(txn_entries)
        submit_gate_station = _submit_gate_station(txn_entries)
        broker_ack_station = _broker_ack_station(txn_entries)
        fill_station = _fill_station(txn_entries)
        signal_station = _signal_station(transaction_ref, decisions, now_ms)
    else:
        _cross_bot_receipt = (
            f"Transaction {transaction_ref!r} belongs to a different bot; "
            "this station cannot show evidence from another bot's order."
        )
        intent_station = _station("INTENT", "blocked", _cross_bot_receipt)
        submit_gate_station = _station("SUBMIT_GATE", "blocked", _cross_bot_receipt)
        broker_ack_station = _station("BROKER_ACK", "blocked", _cross_bot_receipt)
        fill_station = _station("FILL", "blocked", _cross_bot_receipt)
        signal_station = _station("SIGNAL", "blocked", _cross_bot_receipt)

    submitted_at_ms = next(
        (e.recorded_at_ms for e in txn_entries if e.kind is ClerkEntryKind.SUBMIT_ACKED),
        None,
    )

    builders = {
        "SIGNAL": signal_station,
        "INTENT": intent_station,
        "SUBMIT_GATE": submit_gate_station,
        "BROKER_ACK": broker_ack_station,
        "FILL": fill_station,
        "RECONCILED": _reconciled_station(
            latest_reconciliation, now_ms, submitted_at_ms
        ),
    }
    return [builders[station_id] for station_id in STATION_IDS]
