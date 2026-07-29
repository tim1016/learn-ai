"""Read-side custody timing derived from canonical Clerk journal receipts."""

from __future__ import annotations

from collections.abc import Iterable

from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.schemas.clerk_transaction_projection import (
    ClerkCustodyDurations,
    ClerkCustodyTimeline,
    ClerkTransactionRow,
)

_TERMINAL_STATUSES = frozenset({"filled", "cancelled", "apicancelled", "inactive", "rejected"})


def custody_timeline_from_journal_entries(
    entries: Iterable[AccountClerkJournalEntry],
    *,
    now_ms: int,
) -> ClerkCustodyTimeline:
    """Return a timeline without treating journal sequence as causal time.

    The input must contain one intent's journal records. Missing phase facts
    stay ``None``. Broker execution time, local callback arrival, and durable
    record time deliberately remain separate even when callbacks are reordered.
    """

    rows = list(entries)
    recorded = _first_recorded(entry for entry in rows if entry.entry_kind == "recorded")
    if recorded is None or recorded.intent is None:
        return ClerkCustodyTimeline()

    submitting = _first_recorded(entry for entry in rows if entry.entry_kind == "broker_submitting")
    acknowledgements = [entry for entry in rows if entry.entry_kind == "broker_acked"]
    callbacks = [entry for entry in rows if entry.entry_kind == "broker_event" and entry.broker_event is not None]
    terminal = _first_recorded(entry for entry in callbacks if _is_economic_terminal(entry))

    broker_returned = _first_int(
        entry.broker_ack.placed_at_ms
        for entry in acknowledgements
        if entry.broker_ack is not None
    )
    earliest_broker_source = _first_int(
        event.get("exec_time_ms")
        for entry in callbacks
        for event in (entry.broker_event,)
        if event is not None
    )
    first_callback_arrived = _first_int(
        event.get("ts_ms")
        for entry in callbacks
        for event in (entry.broker_event,)
        if event is not None
    )
    first_callback_recorded = _first_int(entry.recorded_at_ms for entry in callbacks)
    a0 = recorded.recorded_at_ms
    broker_write = submitting.recorded_at_ms if submitting is not None else None
    return ClerkCustodyTimeline(
        intent_created_at_ms=recorded.intent.created_at_ms,
        clerk_request_received_at_ms=recorded.clerk_request_received_at_ms,
        clerk_intake_admitted_at_ms=recorded.clerk_intake_admitted_at_ms,
        inbox_fsynced_at_ms=recorded.inbox_fsynced_at_ms,
        a0_custody_accepted_at_ms=a0,
        broker_write_started_at_ms=broker_write,
        broker_call_returned_at_ms=broker_returned,
        broker_ack_recorded_at_ms=_first_int(entry.recorded_at_ms for entry in acknowledgements),
        earliest_broker_source_at_ms=earliest_broker_source,
        first_callback_arrived_at_ms=first_callback_arrived,
        first_callback_recorded_at_ms=first_callback_recorded,
        economic_terminal_recorded_at_ms=terminal.recorded_at_ms if terminal is not None else None,
        durations=ClerkCustodyDurations(
            request_to_intake_ms=_duration(
                recorded.clerk_request_received_at_ms,
                recorded.clerk_intake_admitted_at_ms,
            ),
            intake_to_a0_ms=_duration(recorded.clerk_intake_admitted_at_ms, a0),
            a0_to_broker_write_ms=_duration(a0, broker_write),
            broker_write_to_return_ms=_duration(broker_write, broker_returned),
            broker_return_to_first_callback_ms=_duration(broker_returned, first_callback_arrived),
            terminal_age_ms=_duration(terminal.recorded_at_ms, now_ms) if terminal is not None else None,
        ),
    )


def custody_timeline_from_transaction(
    row: ClerkTransactionRow,
    *,
    now_ms: int,
) -> ClerkCustodyTimeline | None:
    """Rebuild one IBKR detail timeline from its projected immutable receipts."""

    if row.broker != "ibkr":
        return None
    entries_by_seq: dict[int, AccountClerkJournalEntry] = {}
    for receipt in (row.receipt, *(event.receipt for event in row.events)):
        try:
            entry = AccountClerkJournalEntry.model_validate(receipt)
        except (TypeError, ValueError):
            continue
        if entry.intent is None or entry.intent.intent_id != row.intent_id:
            continue
        entries_by_seq[entry.seq] = entry
    if not entries_by_seq:
        return None
    return custody_timeline_from_journal_entries(entries_by_seq.values(), now_ms=now_ms)


def _first_int(values: Iterable[object]) -> int | None:
    observed = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    return min(observed) if observed else None


def _first_recorded(
    entries: Iterable[AccountClerkJournalEntry],
) -> AccountClerkJournalEntry | None:
    return min(entries, key=lambda entry: (entry.recorded_at_ms, entry.seq), default=None)


def _duration(start_ms: int | None, end_ms: int | None) -> int | None:
    if start_ms is None or end_ms is None or end_ms < start_ms:
        return None
    return end_ms - start_ms


def _is_economic_terminal(entry: AccountClerkJournalEntry) -> bool:
    if entry.broker_event is None:
        return False
    status = entry.broker_event.get("status")
    if isinstance(status, str) and status.lower() in _TERMINAL_STATUSES:
        return True
    event_type = entry.broker_event.get("event_type")
    if event_type == "cancel":
        return True
    return event_type == "error" and status in {"Inactive", "Rejected"}


__all__ = ["custody_timeline_from_journal_entries", "custody_timeline_from_transaction"]
