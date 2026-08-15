"""Operator-gated raw-evidence service (spec §14, S4).

Exposes bounded, paged, redacted order-journal evidence for the operator lens.
Every read is audit-logged server-side (who/when/what). Redaction is re-verified
at response time — the capture journal already strips secrets; this layer
confirms no sensitive fields survived.

Design constraints (§14):
- Bounded: max ``PAGE_SIZE_MAX`` entries per request.
- Paged: cursor is the sequence position (int) of the first entry to return.
- Size-capped: the total evidence bytes are bounded per page.
- Redacted: ``order_id``, broker credentials, and raw error stacks are stripped.
  Field-level redaction is applied here, not assumed from the journal.
- Audit-logged: every read call appends to an audit JSONL log.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Final

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.journal import OrderJournal, get_clerk_settings
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.alpaca.clerk.sqlite.projection_models import TimelineEntry
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.broker_v2_evidence import EvidenceAuditEntry, EvidenceEntry, EvidencePage
from app.services.broker_v2_panel.account_projection_owner import get_or_create_owner
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 50

# Human-readable labels for the closed ClerkEntryKind vocabulary.
_KIND_LABELS: dict[str, str] = {
    ClerkEntryKind.INTENT_RECORDED: "Intent recorded",
    ClerkEntryKind.SUBMIT_ACKED: "Submit acknowledged",
    ClerkEntryKind.SUBMIT_FAILED: "Submit failed",
    ClerkEntryKind.CANCEL_RECORDED: "Cancel recorded",
    ClerkEntryKind.CANCEL_ACKED: "Cancel acknowledged",
    ClerkEntryKind.CANCEL_FAILED: "Cancel failed",
    ClerkEntryKind.ORDER_EVENT: "Order event",
    ClerkEntryKind.ACTIVITY_RECOVERY: "Activity recovery",
    ClerkEntryKind.RECONCILIATION: "Reconciliation sweep",
    ClerkEntryKind.HOLD_SET: "Hold set",
    ClerkEntryKind.HOLD_CLEARED: "Hold cleared",
}

_SQLITE_TRANSITION_COPY: Final[dict[str, tuple[str, str]]] = {
    "STRATEGY_INSTANCE_REGISTERED": (
        "Bot registered",
        "The Account Clerk registered this bot identity for custody.",
    ),
    "RUN_STARTED": ("Run started", "The Account Clerk admitted this bot run."),
    "RUN_STOPPED": ("Run stopped", "The Account Clerk stopped this bot run."),
    "COMMAND_REJECTED": ("Command rejected", "The Account Clerk rejected the command."),
    "ENTER_ACCEPTED": ("Entry accepted", "The Account Clerk accepted an entry operation."),
    "EXIT_ACCEPTED": ("Exit accepted", "The Account Clerk accepted an exit operation."),
    "EXIT_REDUCING_ORDER_CREATED": (
        "Reducing order created",
        "The Account Clerk created an order that reduces attributed exposure.",
    ),
    "EXIT_ATTRIBUTED_FLAT": (
        "Attributed exposure flat",
        "Fresh evidence proved this bot's attributed exposure is flat.",
    ),
    "EXIT_NOT_FLAT": (
        "Exit remains open",
        "The exit finished without proof that attributed exposure is flat.",
    ),
    "ORDER_SUBMIT_REQUESTED": (
        "Order submission requested",
        "The Account Clerk recorded the broker-write attempt before contact.",
    ),
    "ORDER_SUBMIT_ACKED": (
        "Order submission acknowledged",
        "The broker acknowledged the exact submitted order identity.",
    ),
    "ORDER_SUBMIT_FAILED": (
        "Order submission failed",
        "The order submission reached a proven terminal failure.",
    ),
    "ORDER_SUBMIT_UNCERTAIN": (
        "Order submission uncertain",
        "The Account Clerk cannot yet prove the broker outcome.",
    ),
    "ORDER_CANCEL_REQUESTED": (
        "Order cancellation requested",
        "The Account Clerk recorded the exact cancellation target before broker contact.",
    ),
    "ORDER_CANCEL_UNCERTAIN": (
        "Order cancellation uncertain",
        "The Account Clerk cannot yet prove the cancellation outcome.",
    ),
    "ORDER_FILL_OBSERVED": (
        "Order fill observed",
        "The Account Clerk recorded broker-authored fill evidence.",
    ),
    "EXECUTION_SLICE_FILLED": (
        "Execution slice recorded",
        "The Account Clerk recorded one broker-reported execution slice.",
    ),
    "EXECUTION_COVERAGE_QUARANTINED": (
        "Execution coverage quarantined",
        "The Account Clerk retained ambiguous exact execution evidence outside effective economics.",
    ),
    "EXECUTION_COVERAGE_SUPERSEDED": (
        "Execution coverage superseded",
        "The Account Clerk replaced one aggregate recovery contribution with matching exact execution evidence.",
    ),
    "EXECUTION_COVERAGE_RESOLVED": (
        "Execution coverage resolved",
        "An operator-approved proof replaced aggregate recovery with exact execution evidence.",
    ),
    "EXECUTION_CORRECTED": (
        "Execution correction recorded",
        "The Account Clerk replaced a prior execution slice with corrected broker evidence.",
    ),
    "EXTERNAL_ORDER_OBSERVED": (
        "External order observed",
        "The Account Clerk recorded an order outside the configured bot namespaces.",
    ),
    "EXTERNAL_ORDER_ACKNOWLEDGED": (
        "External order acknowledged",
        "An operator acknowledged the held external-order evidence.",
    ),
    "ENTRY_TERMINAL_CONFIRMED": (
        "Entry terminal state confirmed",
        "The Account Clerk confirmed the entry order reached a terminal state.",
    ),
    "RECONCILIATION_ATTEMPTED": (
        "Reconciliation completed",
        "The Account Clerk compared durable custody with a broker observation.",
    ),
    "ACCOUNT_HOLD_RAISED": (
        "Account hold raised",
        "The Account Clerk blocked new exposure for the account.",
    ),
    "ACCOUNT_HOLD_REFRESHED": (
        "Account hold refreshed",
        "Fresh evidence confirmed that the account hold remains necessary.",
    ),
    "ACCOUNT_HOLD_RESOLVED": (
        "Account hold resolved",
        "Fresh evidence satisfied the Account Clerk's hold resolution policy.",
    ),
    "UNCERTAINTY_RAISED": (
        "Uncertainty raised",
        "The Account Clerk recorded a new custody uncertainty.",
    ),
    "UNCERTAINTY_REFRESHED": (
        "Uncertainty refreshed",
        "Fresh evidence confirmed that the custody uncertainty remains open.",
    ),
    "UNCERTAINTY_RESOLVED": (
        "Uncertainty resolved",
        "Fresh evidence satisfied the Account Clerk's uncertainty resolution policy.",
    ),
    "CUSTODY_SUBJECT_REGISTERED": (
        "Custody subject registered",
        "The Account Clerk registered the durable economic custody subject.",
    ),
    "MANUAL_TICKET_RESERVED": (
        "Manual ticket reserved",
        "The Account Clerk reserved the manual order ticket before broker contact.",
    ),
    "MANUAL_ORDER_ACCEPTED": (
        "Manual order accepted",
        "The Account Clerk accepted one manual order leg for submission.",
    ),
    "MANUAL_ORDER_CANCEL_ACCEPTED": (
        "Manual cancellation accepted",
        "The Account Clerk accepted a verified manual-order cancellation.",
    ),
    "MANUAL_TICKET_PAUSED_UNKNOWN": (
        "Manual ticket paused",
        "The Account Clerk paused the ticket while a broker outcome remains unknown.",
    ),
    "MANUAL_TICKET_COMPLETED": (
        "Manual ticket completed",
        "The Account Clerk completed every required ticket leg.",
    ),
    "MANUAL_TICKET_CANCELED": (
        "Manual ticket canceled",
        "The Account Clerk recorded the terminal ticket cancellation.",
    ),
    "MANUAL_ORDER_CANCELED": (
        "Manual order canceled",
        "The broker confirmed the manual order cancellation.",
    ),
    "MANUAL_ORDER_FILLED": (
        "Manual order filled",
        "The Account Clerk recorded complete exact fill coverage for the manual order.",
    ),
    "MANUAL_ORDER_TERMINAL": (
        "Manual order terminal",
        "The Account Clerk recorded the manual order's terminal broker state.",
    ),
    "MANUAL_ORDER_CANCEL_CONFIRMED": (
        "Manual cancellation confirmed",
        "The Account Clerk confirmed the manual order was canceled at the broker.",
    ),
    "MANUAL_ORDER_CANCEL_TERMINAL": (
        "Manual cancellation terminal",
        "The Account Clerk recorded the terminal outcome of the manual cancellation.",
    ),
}

_SQLITE_OPERATION_STATE_COPY: Final[dict[str, str]] = {
    "accepted": "Accepted",
    "failed": "Failed",
    "in_progress": "In progress",
    "rejected": "Rejected",
    "succeeded": "Succeeded",
    "unknown": "Outcome unknown",
}

_SQLITE_CUSTODY_COPY: Final[dict[str, str]] = {
    "ACCOUNT_CLERK": "Account Clerk",
}

# A single process-wide lock for audit log appends (same discipline as
# OrderJournal's own _JOURNAL_LOCK).
_AUDIT_LOCK = threading.Lock()


def _audit_log_path(account_id: str) -> Path:
    root = get_clerk_settings().dir
    safe_account = "".join(c for c in account_id if c.isalnum() or c in "-_.")
    return root / "accounts" / safe_account / "evidence_audit.jsonl"


def _append_audit_entry(entry: EvidenceAuditEntry) -> None:
    path = _audit_log_path(entry.account_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = entry.model_dump_json() + "\n"
        with _AUDIT_LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except OSError:
        logger.warning(
            "Evidence audit log write failed",
            extra={"account_id": entry.account_id, "sid": entry.strategy_instance_id},
        )


def _redact_summary(entry: OrderJournalEntry) -> tuple[str, bool]:
    """Build a redacted summary string for one journal entry.

    Returns (summary_text, has_more_detail). ``has_more_detail`` is True when
    the raw entry contains fields beyond what the summary exposes — operators
    who need the full row must retrieve it through an authenticated channel.

    Redaction rules:
    - ``order_id`` (broker-assigned) is never included.
    - Raw error stacks are truncated to a single line.
    - Broker credentials embedded in any field are stripped (none expected here
      since the clerk journal strips them at capture time, but this layer
      re-verifies by never forwarding raw broker response bodies).
    """
    kind = entry.kind
    parts: list[str] = [f"kind={kind}"]
    has_more = False  # default; overridden per-branch below

    if entry.intent_id:
        parts.append(f"intent={entry.intent_id[:32]}")

    if kind is ClerkEntryKind.INTENT_RECORDED:
        if entry.leg:
            parts.append(
                f"{entry.leg.side.value} {entry.leg.quantity} {entry.leg.symbol}"
            )
        has_more = bool(entry.leg)

    elif kind is ClerkEntryKind.SUBMIT_ACKED:
        if entry.order:
            parts.append(f"status={entry.order.status}")
        has_more = bool(entry.order)

    elif kind is ClerkEntryKind.SUBMIT_FAILED:
        # Truncate error to one line; never expose a stack trace.
        msg = (entry.error_message or "")[:120]
        parts.append(f"error={msg}")
        has_more = bool(entry.error_detail)

    elif kind is ClerkEntryKind.ORDER_EVENT:
        if entry.event:
            parts.append(f"event={entry.event.event_type}")
            # BrokerOrderEvent carries per-event quantity/price (not the
            # order-level filled_quantity/filled_avg_price field names).
            if entry.event.quantity:
                parts.append(f"filled={entry.event.quantity}@{entry.event.price}")
        has_more = bool(entry.event)

    elif kind is ClerkEntryKind.RECONCILIATION:
        if entry.verdict:
            parts.append(f"verdict={entry.verdict}")
        has_more = False

    elif kind in (ClerkEntryKind.HOLD_SET, ClerkEntryKind.HOLD_CLEARED):
        if entry.reason_code:
            parts.append(f"reason={entry.reason_code}")
        has_more = bool(entry.reason)

    else:
        has_more = False

    return " | ".join(parts), has_more


def _sqlite_evidence_entry(entry: TimelineEntry) -> EvidenceEntry:
    try:
        kind_label, summary = _SQLITE_TRANSITION_COPY[entry.transition_kind]
        state = _SQLITE_OPERATION_STATE_COPY[entry.operation_state]
        custody = _SQLITE_CUSTODY_COPY[entry.custody_owner]
    except KeyError as exc:
        raise RuntimeError(
            f"SQLite evidence copy is missing for closed code {exc.args[0]!r}"
        ) from exc
    return EvidenceEntry(
        seq=entry.sequence,
        kind=entry.transition_kind,
        kind_label=kind_label,
        recorded_at_ms=entry.recorded_at_ms,
        order_ref=entry.order_ref,
        intent_id=None,
        summary=f"{summary} Current operation state: {state}.",
        has_more_detail=entry.proof_reference is not None,
        operation_ref=entry.operation_ref,
        operation_state=entry.operation_state,
        broker_state=entry.broker_state,
        custody_owner=custody,
        proof_reference=entry.proof_reference,
        source_event_at_ms=entry.source_event_at_ms,
        clerk_observed_at_ms=entry.clerk_observed_at_ms,
    )


def _read_active_sqlite_evidence(
    *,
    account_id: str,
    sid: str,
    transaction_ref: str | None,
    cursor: str | int | None,
    page_size: int,
) -> tuple[list[EvidenceEntry], str | None, int, bool] | None:
    """Read the selected SQLite authority, never a latent database by path."""
    runtime = get_active_clerk_runtime()
    if runtime is None or runtime.authority_kind != "sqlite":
        return None
    clerk = runtime.clerk
    if not isinstance(clerk, SqliteAlpacaClerkFacade):
        raise RuntimeError("Active SQLite Clerk does not expose its verified read authority")
    if clerk.account_id != account_id:
        raise RuntimeError("Active SQLite Clerk account does not match the requested account")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("SQLite evidence requires its opaque cursor")
    reader = SqliteClerkProjectionReader.from_repository(clerk.repository)
    try:
        effect_operation_id = (
            transaction_ref
            if transaction_ref is not None and transaction_ref.startswith("effect:")
            else None
        )
        page = reader.timeline_page(
            strategy_instance_id=sid,
            order_ref=(transaction_ref if effect_operation_id is None else None),
            effect_operation_id=effect_operation_id,
            cursor=cursor,
            page_size=page_size,
        )
    finally:
        reader.close()
    entries = [_sqlite_evidence_entry(entry) for entry in page.entries]
    return entries, page.next_cursor, page.total_entries, page.next_cursor is not None


def read_evidence_page(
    *,
    account_id: str,
    sid: str,
    transaction_ref: str | None,
    cursor: str | int | None,
    page_size: int,
    operator_identity: str,
    client_hint: str | None,
) -> EvidencePage:
    """Return one bounded, redacted, audit-logged evidence page.

    Args:
        account_id: The account whose journal to read.
        sid: Filter to this bot's namespace only.
        transaction_ref: If given, filter further to the selected SQLite
            effect operation or legacy order identity.
        cursor: Sequence position to start from (0-based); ``None`` = from end
            (newest-first, default for operator lens journal tail).
        page_size: Capped at ``PAGE_SIZE_MAX``.
        operator_identity: The configured server-side operator identity (§14).
        client_hint: Optional client-provided label for audit tracing.

    Returns:
        ``EvidencePage`` with redacted entries and the next-page cursor.

    Side effect:
        Appends one ``EvidenceAuditEntry`` to the per-account audit log.
    """
    page_size = min(max(1, page_size), PAGE_SIZE_MAX)
    read_at = now_ms_utc()

    sqlite_page = _read_active_sqlite_evidence(
        account_id=account_id,
        sid=sid,
        transaction_ref=transaction_ref,
        cursor=cursor,
        page_size=page_size,
    )
    if sqlite_page is not None:
        evidence_entries, next_cursor, total, truncated = sqlite_page
        audit = EvidenceAuditEntry(
            account_id=account_id,
            strategy_instance_id=sid,
            transaction_ref=transaction_ref,
            operator_identity=operator_identity,
            read_at_ms=read_at,
            page_cursor=cursor,
            page_size=page_size,
            entries_returned=len(evidence_entries),
            client_hint=client_hint,
        )
        _append_audit_entry(audit)
        return EvidencePage(
            strategy_instance_id=sid,
            account_id=account_id,
            transaction_ref=transaction_ref,
            entries=evidence_entries,
            next_cursor=next_cursor,
            total_entries=total,
            truncated=truncated,
            read_by=operator_identity,
            read_at_ms=read_at,
        )

    journal = OrderJournal(account_id=account_id, root=get_clerk_settings().dir)
    owner = get_or_create_owner(account_id, "alpaca")
    owner.refresh_journal(journal)

    # The account projection owner maintains this namespace index while tailing
    # appended records, so an evidence request never scans the full journal.
    bot_entries = owner.entries_for_sid(sid)
    if transaction_ref:
        bot_entries = [
            e for e in bot_entries if e.order_ref == transaction_ref
        ]

    total = len(bot_entries)

    # Newest-first: reverse so cursor=0 is the most recent entry.
    bot_entries_desc = list(reversed(bot_entries))

    if cursor is not None and not isinstance(cursor, int):
        try:
            start = int(cursor)
        except ValueError as exc:
            raise ValueError("Legacy evidence cursor must be an integer") from exc
    else:
        start = cursor if cursor is not None else 0
    end = start + page_size
    page_slice = bot_entries_desc[start:end]

    evidence_entries: list[EvidenceEntry] = []
    for seq_in_page, entry in enumerate(page_slice):
        summary, has_more = _redact_summary(entry)
        kind_label = _KIND_LABELS.get(entry.kind, entry.kind)
        evidence_entries.append(
            EvidenceEntry(
                seq=start + seq_in_page,
                kind=entry.kind,
                kind_label=kind_label,
                recorded_at_ms=entry.recorded_at_ms,
                order_ref=entry.order_ref,
                intent_id=entry.intent_id,
                summary=summary,
                has_more_detail=has_more,
            )
        )

    next_cursor = end if end < total else None
    truncated = end < total

    audit = EvidenceAuditEntry(
        account_id=account_id,
        strategy_instance_id=sid,
        transaction_ref=transaction_ref,
        operator_identity=operator_identity,
        read_at_ms=read_at,
        page_cursor=cursor,
        page_size=page_size,
        entries_returned=len(evidence_entries),
        client_hint=client_hint,
    )
    _append_audit_entry(audit)

    return EvidencePage(
        strategy_instance_id=sid,
        account_id=account_id,
        transaction_ref=transaction_ref,
        entries=evidence_entries,
        next_cursor=next_cursor,
        total_entries=total,
        truncated=truncated,
        read_by=operator_identity,
        read_at_ms=read_at,
    )
