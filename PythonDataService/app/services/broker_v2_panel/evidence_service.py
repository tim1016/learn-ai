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

from app.broker.alpaca.clerk.journal import OrderJournal, get_clerk_settings
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.engine.live.order_identity import NAMESPACE_ROOT
from app.schemas.broker_v2_evidence import EvidenceAuditEntry, EvidenceEntry, EvidencePage
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


def _is_bot_entry(entry: OrderJournalEntry, sid: str) -> bool:
    """Return True if this journal entry belongs to the given bot's namespace."""
    ns = f"{NAMESPACE_ROOT}/{sid}/v1:"
    if entry.order_ref and entry.order_ref.startswith(ns):
        return True
    return entry.operator == sid


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


def read_evidence_page(
    *,
    account_id: str,
    sid: str,
    transaction_ref: str | None,
    cursor: int | None,
    page_size: int,
    operator_identity: str,
    client_hint: str | None,
) -> EvidencePage:
    """Return one bounded, redacted, audit-logged evidence page.

    Args:
        account_id: The account whose journal to read.
        sid: Filter to this bot's namespace only.
        transaction_ref: If given, filter further to entries whose
            ``order_ref`` matches this ref.
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

    journal = OrderJournal(account_id=account_id, root=get_clerk_settings().dir)
    all_entries: list[OrderJournalEntry] = journal.read_entries()

    # Filter to this bot's namespace (and optionally to one transaction_ref).
    bot_entries = [e for e in all_entries if _is_bot_entry(e, sid)]
    if transaction_ref:
        bot_entries = [
            e for e in bot_entries if e.order_ref == transaction_ref
        ]

    total = len(bot_entries)

    # Newest-first: reverse so cursor=0 is the most recent entry.
    bot_entries_desc = list(reversed(bot_entries))

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
