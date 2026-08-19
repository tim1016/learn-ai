"""Read-compatible historical account-Clerk journal storage and projections."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import overload

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live import durable_append_log
from app.engine.live.account_artifacts import (
    account_artifact_file_path,
    safe_account_artifact_id,
)
from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerAckReceipt,
    AccountClerkBrokerEventReceipt,
    AccountClerkBrokerEvidenceBaseline,
    AccountClerkCustodyStatus,
    AccountClerkEmergencyFlattenReceipt,
    AccountClerkEmergencyOperationEvent,
    AccountClerkInboxEntry,
    AccountClerkIntentRejected,
    AccountClerkJournalCorruptError,
    AccountClerkJournalEntry,
    AccountClerkOperatorAdjustment,
    AccountClerkOperatorAdjustmentConflict,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
)
from app.engine.live.live_state_sidecar import _file_lock

ACCOUNT_CLERK_INBOX_FILENAME = "clerk_inbox.jsonl"
ACCOUNT_CLERK_JOURNAL_FILENAME = "clerk_journal.jsonl"


def account_clerk_inbox_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifact_file_path(
        artifacts_root,
        safe_account_artifact_id(account_id),
        ACCOUNT_CLERK_INBOX_FILENAME,
    )


def account_clerk_journal_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifact_file_path(
        artifacts_root,
        safe_account_artifact_id(account_id),
        ACCOUNT_CLERK_JOURNAL_FILENAME,
    )


def read_account_clerk_inbox(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountClerkInboxEntry]:
    """Read the strict, replayable durable intake inbox for an account."""

    path = account_clerk_inbox_path(artifacts_root, account_id)
    journal_path = account_clerk_journal_path(artifacts_root, account_id)
    with _read_only_journal_lock(journal_path) as coordinated:
        before = _observation_identity(path, journal_path)
        entries = _read_jsonl(path, AccountClerkInboxEntry)
        _assert_unchanged_uncoordinated_observation(
            coordinated,
            before,
            path,
            journal_path,
        )
        return entries


def read_account_clerk_journal(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountClerkJournalEntry]:
    """Read the strict, serial receipt-#1 ledger for an account."""

    path = account_clerk_journal_path(artifacts_root, account_id)
    with _read_only_journal_lock(path) as coordinated:
        before = _observation_identity(path)
        entries = _read_journal_jsonl(path)
        _assert_unchanged_uncoordinated_observation(coordinated, before, path)
        return entries


def read_account_clerk_durability_spine(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountClerkJournalEntry]:
    """Strictly validate the paired journal/inbox durability spine."""

    inbox_path = account_clerk_inbox_path(artifacts_root, account_id)
    journal_path = account_clerk_journal_path(artifacts_root, account_id)
    if not journal_path.exists() and not inbox_path.exists():
        return []
    with _read_only_journal_lock(journal_path) as coordinated:
        before = _observation_identity(inbox_path, journal_path)
        entries = read_account_clerk_durability_spine_locked(inbox_path, journal_path)
        _assert_unchanged_uncoordinated_observation(
            coordinated,
            before,
            inbox_path,
            journal_path,
        )
        return entries


def read_account_clerk_journal_locked(path: Path) -> list[AccountClerkJournalEntry]:
    """Strictly replay a journal while the caller holds its writer lock.

    The corruption ceremony uses this seam so validation and the irreversible
    rename occur under one lock; a second writer cannot repair the file in the
    gap between deciding it is corrupt and quarantining it.
    """

    return _read_journal_jsonl(path)


def read_account_clerk_durability_spine_locked(
    inbox_path: Path,
    journal_path: Path,
) -> list[AccountClerkJournalEntry]:
    """Strictly validate both Clerk durability artifacts under their writer lock."""

    journal_entries = _read_journal_jsonl(journal_path)
    inbox_entries = _validate_inbox_replayable(
        _read_jsonl(inbox_path, AccountClerkInboxEntry),
        journal_entries,
        journal_path,
    )
    entries_by_seq = {entry.seq: entry for entry in journal_entries}
    for inbox_entry in inbox_entries:
        entries_by_seq.setdefault(
            inbox_entry.seq,
            AccountClerkJournalEntry(
                seq=inbox_entry.seq,
                recorded_at_ms=inbox_entry.received_at_ms,
                intent=inbox_entry.intent,
                clerk_request_received_at_ms=inbox_entry.clerk_request_received_at_ms,
                async_custody_lane=inbox_entry.async_custody_lane,
                effect_evidence=inbox_entry.effect_evidence,
            ),
        )
    return [entries_by_seq[seq] for seq in sorted(entries_by_seq)]


def fold_account_clerk_custody_statuses(
    entries: list[AccountClerkJournalEntry],
) -> tuple[AccountClerkCustodyStatus, ...]:
    """Purely fold bounded custody stages from a validated durability spine."""

    by_intent_id: dict[str, list[AccountClerkJournalEntry]] = {}
    for entry in entries:
        if entry.intent is not None:
            by_intent_id.setdefault(entry.intent.intent_id, []).append(entry)
    statuses = [
        status
        for intent_entries in by_intent_id.values()
        if (status := _custody_status_for_entries(intent_entries)) is not None
    ]
    return tuple(sorted(statuses, key=lambda status: status.intent_id))


def seed_account_clerk_broker_evidence_baseline(
    artifacts_root: Path,
    account_id: str,
    baseline: AccountClerkBrokerEvidenceBaseline,
) -> None:
    """Durably seed a fresh post-quarantine journal with unowned broker facts."""

    path = account_clerk_journal_path(artifacts_root, account_id)
    with _file_lock(path):
        seed_account_clerk_broker_evidence_baseline_locked(path, account_id, baseline)


def seed_account_clerk_broker_evidence_baseline_locked(
    path: Path,
    account_id: str,
    baseline: AccountClerkBrokerEvidenceBaseline,
) -> None:
    """Create or verify precisely one fresh broker-evidence baseline.

    A retry after a crash may find the baseline durable before the recovery
    state reached COMPLETE.  Accept only byte-for-contract equivalent evidence,
    never a second snapshot or an arbitrary pre-existing journal.
    """

    if baseline.account_id != account_id:
        raise ValueError("broker-evidence baseline account must match its journal account")
    if path.exists() and path.read_bytes():
        entries = _read_journal_jsonl(path)
        if (
            len(entries) == 1
            and entries[0].entry_kind == "broker_evidence_baseline"
            and entries[0].broker_evidence_baseline == baseline
        ):
            return
        raise AccountClerkJournalCorruptError(path, "re-baseline requires an empty or matching fresh journal")
    _append_jsonl(
        path,
        AccountClerkJournalEntry(
            seq=1,
            entry_kind="broker_evidence_baseline",
            recorded_at_ms=baseline.observed_at_ms,
            broker_evidence_baseline=baseline,
        ),
    )


def inspect_account_clerk_journal(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountClerkJournalEntry]:
    """Strictly inspect an existing journal without creating an account directory.

    Desk projections remain observational for a previously unseen account.
    Once a journal exists, coordinate with its writer lock so a projection
    never parses a partially appended JSONL row.
    """

    safe_account_id = safe_account_artifact_id(account_id)
    inbox_path = account_clerk_inbox_path(artifacts_root, safe_account_id)
    journal_path = account_clerk_journal_path(artifacts_root, safe_account_id)
    if not journal_path.exists() and not inbox_path.exists():
        return []
    # The sibling lock already exists for an established journal; taking it
    # keeps this observational projection from parsing a partially appended
    # JSONL row while still avoiding directory creation for unseen accounts.
    with _read_only_journal_lock(journal_path) as coordinated:
        before = _observation_identity(inbox_path, journal_path)
        entries = read_account_clerk_durability_spine_locked(inbox_path, journal_path)
        _assert_unchanged_uncoordinated_observation(
            coordinated,
            before,
            inbox_path,
            journal_path,
        )
        return entries


def recovery_operation_started_for_namespace(
    entries: list[AccountClerkJournalEntry],
    namespace: str,
) -> bool:
    """Whether a namespace has crossed an unresolved recovery broker boundary."""

    terminal_intent_ids = {
        entry.intent.intent_id for entry in entries if entry.intent is not None and entry.entry_kind == "broker_acked"
    }
    return any(
        entry.intent is not None
        and entry.intent.bot_order_namespace == namespace
        and entry.intent.intent_id not in terminal_intent_ids
        and entry.entry_kind in {"recovery_cancelling", "recovery_cancelled", "broker_submitting", "broker_uncertain"}
        for entry in entries
    )


@overload
def _read_jsonl(path: Path, model_type: type[AccountClerkInboxEntry]) -> list[AccountClerkInboxEntry]: ...


@overload
def _read_jsonl(path: Path, model_type: type[AccountClerkJournalEntry]) -> list[AccountClerkJournalEntry]: ...


def _read_jsonl(
    path: Path,
    model_type: type[AccountClerkInboxEntry] | type[AccountClerkJournalEntry],
) -> list[AccountClerkInboxEntry] | list[AccountClerkJournalEntry]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise AccountClerkJournalCorruptError(path, f"invalid UTF-8: {exc}") from exc

    entries = []
    for line_no, line in enumerate(lines, start=1):
        if not line:
            raise AccountClerkJournalCorruptError(path, f"blank row at line {line_no}")
        try:
            entry = model_type.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise AccountClerkJournalCorruptError(path, f"invalid row at line {line_no}: {exc}") from exc
        entries.append(entry)
    return entries


def _read_journal_jsonl(path: Path) -> list[AccountClerkJournalEntry]:
    entries = _read_jsonl(path, AccountClerkJournalEntry)
    expected_seq = 1
    for line_no, entry in enumerate(entries, start=1):
        if entry.seq != expected_seq:
            raise AccountClerkJournalCorruptError(
                path,
                f"expected seq {expected_seq} at line {line_no}, found {entry.seq}",
            )
        expected_seq += 1
    return entries


def _validate_inbox_replayable(
    inbox_entries: list[AccountClerkInboxEntry],
    journal_entries: list[AccountClerkJournalEntry],
    journal_path: Path,
) -> list[AccountClerkInboxEntry]:
    """Prove that inbox rows can only replay the Clerk's next durable intents."""

    unique_inbox_entries: list[AccountClerkInboxEntry] = []
    inbox_by_seq: dict[int, AccountClerkInboxEntry] = {}
    for inbox_entry in inbox_entries:
        existing_inbox_entry = inbox_by_seq.get(inbox_entry.seq)
        if existing_inbox_entry is None:
            inbox_by_seq[inbox_entry.seq] = inbox_entry
            unique_inbox_entries.append(inbox_entry)
            continue
        if existing_inbox_entry != inbox_entry:
            raise AccountClerkJournalCorruptError(
                journal_path,
                f"duplicate incompatible inbox rows at seq {inbox_entry.seq}",
            )

    journal_by_seq = {entry.seq: entry for entry in journal_entries}
    expected_seq = _next_seq(journal_entries)
    for inbox_entry in unique_inbox_entries:
        journal_entry = journal_by_seq.get(inbox_entry.seq)
        if journal_entry is not None:
            if journal_entry.intent != inbox_entry.intent:
                raise AccountClerkJournalCorruptError(
                    journal_path,
                    f"inbox and journal intent differ at seq {inbox_entry.seq}",
                )
            if (
                journal_entry.clerk_request_received_at_ms != inbox_entry.clerk_request_received_at_ms
                or journal_entry.async_custody_lane != inbox_entry.async_custody_lane
                or journal_entry.effect_evidence != inbox_entry.effect_evidence
            ):
                raise AccountClerkJournalCorruptError(
                    journal_path,
                    f"inbox and journal A0 admission proof differs at seq {inbox_entry.seq}",
                )
            continue
        if inbox_entry.seq != expected_seq:
            raise AccountClerkJournalCorruptError(
                journal_path,
                f"inbox seq {inbox_entry.seq} cannot follow journal seq {expected_seq - 1}",
            )
        journal_by_seq[inbox_entry.seq] = AccountClerkJournalEntry(
            seq=inbox_entry.seq,
            recorded_at_ms=inbox_entry.received_at_ms,
            intent=inbox_entry.intent,
            clerk_request_received_at_ms=inbox_entry.clerk_request_received_at_ms,
            async_custody_lane=inbox_entry.async_custody_lane,
            effect_evidence=inbox_entry.effect_evidence,
        )
        expected_seq += 1
    return unique_inbox_entries


def _append_jsonl(path: Path, entry: AccountClerkInboxEntry | AccountClerkJournalEntry) -> None:
    durable_append_log.append_jsonl_record(
        path,
        entry.model_dump_json(),
        trusted_root=path.parent.parent,
    )


def normalize_broker_event(
    event: IbkrOrderEvent | Mapping[str, object],
) -> IbkrOrderEvent | None:
    """Validate the one broker-event model consumed by journal and drain paths."""

    try:
        return IbkrOrderEvent.model_validate(event)
    except (TypeError, ValidationError, ValueError):
        return None


def _next_seq(entries: list[AccountClerkJournalEntry]) -> int:
    return entries[-1].seq + 1 if entries else 1


def _custody_status_for_entries(
    entries: list[AccountClerkJournalEntry],
) -> AccountClerkCustodyStatus | None:
    """Fold lifecycle receipts without making journal sequence a broker-time claim."""

    recorded = next((entry for entry in entries if entry.entry_kind == "recorded"), None)
    if recorded is None or recorded.intent is None:
        return None
    intent = recorded.intent
    # ``async_custody_lane`` is part of the recorded A0 receipt. The legacy
    # marker fallback is retained solely so a journal produced by the earliest
    # unshipped shadow implementation remains readable.
    legacy_queued = next((entry for entry in entries if entry.entry_kind == "custody_queued"), None)
    broker_acked = next((entry for entry in entries if entry.entry_kind == "broker_acked"), None)
    is_terminal = any(
        entry.entry_kind == "broker_event" and is_economic_terminal_broker_event(entry.broker_event)
        for entry in entries
    )
    updated_at_ms = max(entry.recorded_at_ms for entry in entries)
    lane = recorded.async_custody_lane or (legacy_queued.custody_lane if legacy_queued is not None else None)
    if lane is None:
        # The custody read surface must not reinterpret an ordinary synchronous
        # recorded receipt as an asynchronous ownership transfer.
        return None
    if is_terminal:
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A3_ECONOMIC_TERMINAL",
            lifecycle_state="economic_terminal",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            broker_acked=(
                AccountClerkBrokerAckReceipt.from_journal_entry(broker_acked)
                if broker_acked is not None and broker_acked.order_id is not None
                else None
            ),
            updated_at_ms=updated_at_ms,
        )
    if any(entry.entry_kind == "custody_expired_before_submit" for entry in entries):
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A3_ECONOMIC_TERMINAL",
            lifecycle_state="expired_before_submit",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            updated_at_ms=updated_at_ms,
        )
    cancelled_before_submit = next(
        (entry for entry in entries if entry.entry_kind == "custody_cancelled_before_submit"),
        None,
    )
    if cancelled_before_submit is not None:
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A3_ECONOMIC_TERMINAL",
            lifecycle_state="cancelled_before_submit",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            updated_at_ms=cancelled_before_submit.recorded_at_ms,
        )
    if any(entry.entry_kind == "custody_recovery_action_required" for entry in entries):
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A0_CUSTODY_ACCEPTED",
            lifecycle_state="recovery_action_required",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            updated_at_ms=updated_at_ms,
        )
    if any(entry.entry_kind == "custody_submission_hold" for entry in entries):
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A0_CUSTODY_ACCEPTED",
            lifecycle_state="submission_hold",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            updated_at_ms=updated_at_ms,
        )
    if any(entry.entry_kind == "broker_uncertain" for entry in entries):
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A1_BROKER_WRITE_STARTED",
            lifecycle_state="uncertain_requires_reconciliation",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            updated_at_ms=updated_at_ms,
        )
    if broker_acked is not None and broker_acked.order_id is not None:
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A2_BROKER_KNOWN",
            lifecycle_state="broker_known",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            broker_acked=AccountClerkBrokerAckReceipt.from_journal_entry(broker_acked),
            updated_at_ms=updated_at_ms,
        )
    if any(entry.entry_kind == "broker_submitting" for entry in entries):
        return AccountClerkCustodyStatus(
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            custody_stage="A1_BROKER_WRITE_STARTED",
            lifecycle_state="submitting",
            lane=lane,
            recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
            updated_at_ms=updated_at_ms,
        )
    return AccountClerkCustodyStatus(
        account_id=intent.account_id,
        intent_id=intent.intent_id,
        order_ref=intent.order_ref,
        custody_stage="A0_CUSTODY_ACCEPTED",
        lifecycle_state="queued" if lane is not None else "recorded",
        lane=lane,
        recorded=AccountClerkRecordedReceipt.from_journal_entry(recorded),
        updated_at_ms=updated_at_ms,
    )


def is_economic_terminal_broker_event(event: dict[str, object] | None) -> bool:
    if event is None:
        return False
    status = event.get("status")
    if isinstance(status, str) and status.lower() in {
        "filled",
        "cancelled",
        "apicancelled",
        "inactive",
        "rejected",
    }:
        return True
    return event.get("event_type") == "cancel"


@contextlib.contextmanager
def _read_only_journal_lock(journal_path: Path) -> Iterator[bool]:
    """Coordinate an observational read without ever creating a lock artifact."""

    lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
    try:
        handle = open(lock_path, "rb")  # noqa: SIM115
    except FileNotFoundError:
        yield False
        return
    try:
        if sys.platform == "win32":
            yield False
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _observation_identity(*paths: Path) -> tuple[tuple[int, int, int, int] | None, ...]:
    return tuple(_journal_file_identity(path) for path in paths)


def _assert_unchanged_uncoordinated_observation(
    coordinated: bool,
    before: tuple[tuple[int, int, int, int] | None, ...],
    *paths: Path,
) -> None:
    if not coordinated and before != _observation_identity(*paths):
        raise AccountClerkJournalCorruptError(
            paths[-1],
            "Clerk durability artifacts changed during an uncoordinated read",
        )


def _journal_file_identity(path: Path) -> tuple[int, int, int, int] | None:
    """Return a stable file revision so cached Clerk state cannot hide a write."""

    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


__all__ = [
    "ACCOUNT_CLERK_INBOX_FILENAME",
    "ACCOUNT_CLERK_JOURNAL_FILENAME",
    "AccountClerkBrokerAckReceipt",
    "AccountClerkBrokerEventReceipt",
    "AccountClerkCustodyStatus",
    "AccountClerkEmergencyFlattenReceipt",
    "AccountClerkEmergencyOperationEvent",
    "AccountClerkInboxEntry",
    "AccountClerkIntentRejected",
    "AccountClerkJournalCorruptError",
    "AccountClerkJournalEntry",
    "AccountClerkOperatorAdjustment",
    "AccountClerkOperatorAdjustmentConflict",
    "AccountClerkRecordedReceipt",
    "AccountClerkRecoveryFlattenReceipt",
    "account_clerk_inbox_path",
    "account_clerk_journal_path",
    "fold_account_clerk_custody_statuses",
    "is_economic_terminal_broker_event",
    "normalize_broker_event",
    "read_account_clerk_durability_spine",
    "read_account_clerk_durability_spine_locked",
    "read_account_clerk_inbox",
    "read_account_clerk_journal",
    "read_account_clerk_journal_locked",
    "seed_account_clerk_broker_evidence_baseline",
    "seed_account_clerk_broker_evidence_baseline_locked",
]
