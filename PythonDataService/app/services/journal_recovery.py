"""Durable quarantine and broker-evidence re-baseline for corrupt Clerk journals."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path

from app.broker.ibkr.models import IbkrPositionsSnapshot
from app.engine.live.account_artifacts import append_account_event
from app.engine.live.account_clerk_journal import (
    account_clerk_inbox_path,
    account_clerk_journal_path,
    read_account_clerk_durability_spine_locked,
    seed_account_clerk_broker_evidence_baseline_locked,
)
from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerEvidenceBaseline,
    AccountClerkJournalCorruptError,
    AccountClerkPositionEvidence,
)
from app.engine.live.journal_recovery_state import (
    journal_recovery_admission_lock,
    journal_recovery_state_path,
    read_journal_recovery_state,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.artifact_io import atomic_write_pydantic_artifact
from app.schemas.journal_recovery import (
    JournalRecoveryPosition,
    JournalRecoveryReceipt,
    JournalRecoveryState,
)
from app.utils.timestamps import now_ms_utc


class JournalRecoveryError(ValueError):
    """The requested ceremony transition is not currently safe."""


class JournalRecoveryService:
    """Own the filesystem-only recovery ceremony; it never writes to a broker."""

    def __init__(self, *, artifacts_root: Path, now_ms: Callable[[], int] = now_ms_utc) -> None:
        self._artifacts_root = artifacts_root
        self._now_ms = now_ms

    def state(self, *, account_id: str) -> JournalRecoveryState:
        """Return strict durable state or the initial operator-required step."""

        state = read_journal_recovery_state(self._artifacts_root, account_id)
        return state or JournalRecoveryState(account_id=account_id, phase="QUARANTINE_REQUIRED")

    def quarantine(self, *, account_id: str, idempotency_key: str) -> JournalRecoveryReceipt:
        """Claim recovery only after any admitted Clerk broker write has returned."""

        with journal_recovery_admission_lock(self._artifacts_root, account_id):
            return self._quarantine_under_recovery_admission(
                account_id=account_id,
                idempotency_key=idempotency_key,
            )

    def _quarantine_under_recovery_admission(
        self,
        *,
        account_id: str,
        idempotency_key: str,
    ) -> JournalRecoveryReceipt:
        """Retain a verified-corrupt journal without a write/rename race."""

        journal_path = account_clerk_journal_path(self._artifacts_root, account_id)
        inbox_path = account_clerk_inbox_path(self._artifacts_root, account_id)
        with _file_lock(journal_path):
            state = self.state(account_id=account_id)
            if state.phase == "COMPLETE":
                # A later physical corruption is a new operator ceremony, not
                # a replay of yesterday's completed receipt.  Keep both
                # quarantined artifacts and give the fresh operation its own
                # receipts so account events remain deduplicated correctly.
                if self._durability_spine_requires_quarantine_locked(inbox_path, journal_path):
                    state = self._begin_quarantine(
                        account_id=account_id,
                        journal_path=journal_path,
                        idempotency_key=idempotency_key,
                        recovery_epoch=state.recovery_epoch + 1,
                    )
                    self._write_state(account_id, state)
                else:
                    self._require_replay_key(
                        state.quarantine_idempotency_key,
                        idempotency_key,
                        "JOURNAL_RECOVERY_QUARANTINE_IDEMPOTENCY_CONFLICT",
                    )
                    self._append_quarantine_event(state)
                    return self._quarantine_receipt(state)
            elif state.phase in {"REBASELINE_REQUIRED", "REBASELINE_PENDING"}:
                self._require_replay_key(
                    state.quarantine_idempotency_key,
                    idempotency_key,
                    "JOURNAL_RECOVERY_QUARANTINE_IDEMPOTENCY_CONFLICT",
                )
            if state.phase == "QUARANTINE_REQUIRED":
                try:
                    # Strict replay and the irreversible rename share one
                    # journal lock. A second Clerk writer cannot change the
                    # evidence between the decision and the quarantine.
                    read_account_clerk_durability_spine_locked(inbox_path, journal_path)
                except AccountClerkJournalCorruptError:
                    pass
                else:
                    raise JournalRecoveryError("JOURNAL_RECOVERY_JOURNAL_NOT_CORRUPT")
                state = self._begin_quarantine(
                    account_id=account_id,
                    journal_path=journal_path,
                    idempotency_key=idempotency_key,
                    recovery_epoch=state.recovery_epoch,
                )
                # Claim before rename: a crash now remains fenced, never
                # reopens broker writes while the artifact is ambiguous.
                self._write_state(account_id, state)

            if state.phase in {"REBASELINE_REQUIRED", "REBASELINE_PENDING", "COMPLETE"}:
                self._append_quarantine_event(state)
                return self._quarantine_receipt(state)
            if state.phase != "QUARANTINE_PENDING":
                raise JournalRecoveryError("JOURNAL_RECOVERY_QUARANTINE_STATE_INCOMPLETE")
            if state.quarantined_at_ms is None:
                raise JournalRecoveryError("JOURNAL_RECOVERY_QUARANTINE_STATE_INCOMPLETE")

            if state.quarantined_journal_name is not None:
                target = journal_path.with_name(state.quarantined_journal_name)
                if journal_path.exists():
                    if target.exists():
                        raise JournalRecoveryError("JOURNAL_RECOVERY_QUARANTINE_NAME_COLLISION")
                    os.replace(journal_path, target)
                    _fsync_parent_dir(journal_path)
                elif not target.exists():
                    raise JournalRecoveryError("JOURNAL_RECOVERY_CORRUPT_ARTIFACT_MISSING")

            # The inbox is a paired, durable pre-journal boundary. It cannot
            # survive the reset: replaying a pre-corruption row into the fresh
            # baseline would fabricate Clerk ownership for broker evidence.
            if state.quarantined_inbox_name is not None:
                inbox_target = inbox_path.with_name(state.quarantined_inbox_name)
                if inbox_path.exists():
                    if inbox_target.exists():
                        raise JournalRecoveryError("JOURNAL_RECOVERY_QUARANTINE_INBOX_NAME_COLLISION")
                    os.replace(inbox_path, inbox_target)
                    _fsync_parent_dir(inbox_path)
                elif not inbox_target.exists():
                    raise JournalRecoveryError("JOURNAL_RECOVERY_QUARANTINE_INBOX_MISSING")

            next_state = state.model_copy(update={"phase": "REBASELINE_REQUIRED"})
            self._write_state(account_id, next_state)
            self._append_quarantine_event(next_state)
            return self._quarantine_receipt(next_state)

    def rebaseline(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        snapshot: IbkrPositionsSnapshot | None,
    ) -> JournalRecoveryReceipt:
        """Keep re-baseline separate from any concurrent Clerk broker write."""

        with journal_recovery_admission_lock(self._artifacts_root, account_id):
            return self._rebaseline_under_recovery_admission(
                account_id=account_id,
                idempotency_key=idempotency_key,
                snapshot=snapshot,
            )

    def _rebaseline_under_recovery_admission(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        snapshot: IbkrPositionsSnapshot | None,
    ) -> JournalRecoveryReceipt:
        """Seed exactly one immutable broker-evidence baseline, crash-safely."""

        journal_path = account_clerk_journal_path(self._artifacts_root, account_id)
        inbox_path = account_clerk_inbox_path(self._artifacts_root, account_id)
        with _file_lock(journal_path):
            state = self.state(account_id=account_id)
            if state.phase == "COMPLETE":
                self._require_replay_key(
                    state.rebaseline_idempotency_key,
                    idempotency_key,
                    "JOURNAL_RECOVERY_REBASELINE_IDEMPOTENCY_CONFLICT",
                )
                self._append_rebaseline_event(state)
                return self._rebaseline_receipt(state)
            if state.phase not in {"REBASELINE_REQUIRED", "REBASELINE_PENDING"}:
                raise JournalRecoveryError("JOURNAL_RECOVERY_QUARANTINE_REQUIRED")

            if state.phase == "REBASELINE_REQUIRED":
                if snapshot is None or snapshot.account_id != account_id or snapshot.used_cache_fallback:
                    raise JournalRecoveryError("JOURNAL_RECOVERY_FRESH_BROKER_SNAPSHOT_REQUIRED")
                if not snapshot.is_paper:
                    raise JournalRecoveryError("JOURNAL_RECOVERY_PAPER_BROKER_REQUIRED")
                if any(not math.isfinite(position.quantity) for position in snapshot.positions):
                    raise JournalRecoveryError("JOURNAL_RECOVERY_INVALID_BROKER_SNAPSHOT")
                positions = tuple(
                    JournalRecoveryPosition(symbol=position.symbol, signed_quantity=position.quantity)
                    for position in sorted(snapshot.positions, key=lambda position: position.symbol)
                    if position.quantity != 0
                )
                state = JournalRecoveryState(
                    account_id=account_id,
                    recovery_epoch=state.recovery_epoch,
                    phase="REBASELINE_PENDING",
                    quarantined_journal_name=state.quarantined_journal_name,
                    quarantined_inbox_name=state.quarantined_inbox_name,
                    missing_artifacts=state.missing_artifacts,
                    quarantined_at_ms=state.quarantined_at_ms,
                    quarantine_receipt_id=state.quarantine_receipt_id,
                    quarantine_idempotency_key=state.quarantine_idempotency_key,
                    baseline_receipt_id=self._receipt_id(
                        recovery_epoch=state.recovery_epoch,
                        step="rebaseline",
                        idempotency_key=idempotency_key,
                    ),
                    rebaseline_idempotency_key=idempotency_key,
                    broker_evidence_positions=positions,
                    observed_at_ms=snapshot.fetched_at_ms,
                )
                # Persist the exact snapshot and its idempotency receipt before
                # changing the new journal. A retry resumes this plan without
                # taking a different broker snapshot.
                self._write_state(account_id, state)

            if inbox_path.exists() and inbox_path.read_bytes():
                raise JournalRecoveryError("JOURNAL_RECOVERY_FRESH_INBOX_REQUIRED")
            seed_account_clerk_broker_evidence_baseline_locked(
                journal_path,
                account_id,
                self._baseline_from_state(state),
            )
            next_state = state.model_copy(update={"phase": "COMPLETE"})
            self._write_state(account_id, next_state)
            self._append_rebaseline_event(next_state)
            return self._rebaseline_receipt(next_state)

    @staticmethod
    def _require_replay_key(recorded_key: str | None, key: str, conflict: str) -> None:
        if recorded_key != key:
            raise JournalRecoveryError(conflict)

    def _begin_quarantine(
        self,
        *,
        account_id: str,
        journal_path: Path,
        idempotency_key: str,
        recovery_epoch: int,
    ) -> JournalRecoveryState:
        """Durably describe one new rename before touching its source file."""

        recorded_at_ms = self._now_ms()
        suffix = f"{recorded_at_ms}" if recovery_epoch == 1 else f"{recorded_at_ms}-recovery-{recovery_epoch}"
        inbox_path = account_clerk_inbox_path(self._artifacts_root, account_id)
        journal_exists = journal_path.exists()
        inbox_exists = inbox_path.exists()
        return JournalRecoveryState(
            account_id=account_id,
            recovery_epoch=recovery_epoch,
            phase="QUARANTINE_PENDING",
            quarantined_journal_name=(
                f"{journal_path.name}.corrupt-{suffix}" if journal_exists else None
            ),
            quarantined_inbox_name=(
                f"{inbox_path.name}.corrupt-{suffix}"
                if inbox_exists
                else None
            ),
            missing_artifacts=tuple(
                artifact
                for artifact, exists in (("journal", journal_exists), ("inbox", inbox_exists))
                if not exists
            ),
            quarantined_at_ms=recorded_at_ms,
            quarantine_receipt_id=self._receipt_id(
                recovery_epoch=recovery_epoch,
                step="quarantine",
                idempotency_key=idempotency_key,
            ),
            quarantine_idempotency_key=idempotency_key,
        )

    @staticmethod
    def _receipt_id(*, recovery_epoch: int, step: str, idempotency_key: str) -> str:
        prefix = "journal-recovery" if recovery_epoch == 1 else f"journal-recovery-{recovery_epoch}"
        return f"{prefix}-{step}:{idempotency_key}"

    @staticmethod
    def _durability_spine_requires_quarantine_locked(inbox_path: Path, journal_path: Path) -> bool:
        """Treat a missing or unreadable post-recovery spine as fresh corruption."""

        if not journal_path.exists():
            return True
        try:
            read_account_clerk_durability_spine_locked(inbox_path, journal_path)
        except AccountClerkJournalCorruptError:
            return True
        return False

    def _append_quarantine_event(self, state: JournalRecoveryState) -> None:
        receipt = self._quarantine_receipt(state)
        append_account_event(self._artifacts_root, state.account_id, {
            "event_type": "account_clerk_journal_quarantined",
            "receipt_id": receipt.receipt_id,
            "quarantined_journal_name": receipt.quarantined_journal_name,
            "quarantined_inbox_name": state.quarantined_inbox_name,
            "missing_artifacts": state.missing_artifacts,
            "recorded_at_ms": receipt.recorded_at_ms,
        }, only_if_receipt_absent=True)

    def _append_rebaseline_event(self, state: JournalRecoveryState) -> None:
        receipt = self._rebaseline_receipt(state)
        append_account_event(self._artifacts_root, state.account_id, {
            "event_type": "account_clerk_journal_rebaselined",
            "receipt_id": receipt.receipt_id,
            "recorded_at_ms": receipt.recorded_at_ms,
            "broker_evidence_only": True,
            "position_count": len(state.broker_evidence_positions),
            "snapshot_observed_at_ms": state.observed_at_ms,
        }, only_if_receipt_absent=True)

    @staticmethod
    def _baseline_from_state(state: JournalRecoveryState) -> AccountClerkBrokerEvidenceBaseline:
        if state.observed_at_ms is None:
            raise JournalRecoveryError("JOURNAL_RECOVERY_REBASELINE_STATE_INCOMPLETE")
        return AccountClerkBrokerEvidenceBaseline(
            account_id=state.account_id,
            observed_at_ms=state.observed_at_ms,
            positions=tuple(
                AccountClerkPositionEvidence(
                    symbol=position.symbol,
                    signed_quantity=position.signed_quantity,
                    evidence_observed_at_ms=state.observed_at_ms,
                )
                for position in state.broker_evidence_positions
            ),
        )

    def _state_path(self, account_id: str) -> Path:
        return journal_recovery_state_path(self._artifacts_root, account_id)

    def _write_state(self, account_id: str, state: JournalRecoveryState) -> None:
        """Durably advance the ceremony before its following irreversible step."""

        path = self._state_path(account_id)
        atomic_write_pydantic_artifact(path, state)
        _fsync_parent_dir(path)

    def _quarantine_receipt(self, state: JournalRecoveryState) -> JournalRecoveryReceipt:
        if (
            state.phase == "QUARANTINE_REQUIRED"
            or state.quarantine_receipt_id is None
            or state.quarantined_at_ms is None
        ):
            raise JournalRecoveryError("JOURNAL_RECOVERY_QUARANTINE_STATE_INCOMPLETE")
        return JournalRecoveryReceipt(
            receipt_id=state.quarantine_receipt_id,
            account_id=state.account_id,
            phase="REBASELINE_REQUIRED",
            recorded_at_ms=state.quarantined_at_ms,
            quarantined_journal_name=state.quarantined_journal_name,
        )

    def _rebaseline_receipt(self, state: JournalRecoveryState) -> JournalRecoveryReceipt:
        if state.phase != "COMPLETE" or state.baseline_receipt_id is None or state.observed_at_ms is None:
            raise JournalRecoveryError("JOURNAL_RECOVERY_REBASELINE_STATE_INCOMPLETE")
        return JournalRecoveryReceipt(
            receipt_id=state.baseline_receipt_id,
            account_id=state.account_id,
            phase="COMPLETE",
            recorded_at_ms=state.observed_at_ms,
            quarantined_journal_name=state.quarantined_journal_name,
            broker_evidence_positions=state.broker_evidence_positions,
        )
