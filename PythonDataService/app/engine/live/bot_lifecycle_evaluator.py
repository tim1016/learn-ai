"""The sole durable writer for a bot's duty lifecycle and control intent.

Routers and the CLI submit commands here.  The host daemon reports process
facts here after it actuates; it never chooses a phase itself.  Every accepted
transition is recorded twice: as the current atomic state projection and as a
crash-replayable disposition receipt under the same strategy-instance root.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.bot_lifecycle_fence import bot_lifecycle_operation_fence
from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateRecord,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateRecord,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.durable_append_log import append_jsonl_record
from app.engine.live.identity import (
    safe_strategy_instance_path_segment,
    strategy_instance_artifact_dir,
)
from app.engine.live.live_state_sidecar import _file_lock


class LifecycleDispositionAction(StrEnum):
    # A PENDING START_ACCEPTED receipt is the evaluator-owned start prepare.
    # Its terminal receipt is written only after the host daemon is observed
    # to have accepted or rejected the process actuation.
    START_ACCEPTED = "START_ACCEPTED"
    ROSTER_CHANGED = "ROSTER_CHANGED"
    TERMINAL_OUTCOME = "TERMINAL_OUTCOME"
    RETIRED = "RETIRED"
    REOPENED_FOR_DEPLOY = "REOPENED_FOR_DEPLOY"
    DESIRED_STATE_SET = "DESIRED_STATE_SET"
    DEFAULT_DESIRED_STATE_SEEDED = "DEFAULT_DESIRED_STATE_SEEDED"


class LifecycleTransitionRefusedError(RuntimeError):
    """Raised when evidence cannot legally produce the requested transition."""


class LifecycleDispositionCorruptError(RuntimeError):
    """Raised when the evaluator's append-only receipt log cannot be replayed."""


class LifecycleDispositionReceipt(BaseModel):
    """One write-ahead or completed evaluator decision.

    Sequence rules deliberately stay here rather than in
    ``durable_append_log``: this is the lifecycle domain's audit protocol.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    receipt_id: str = Field(min_length=1, max_length=256)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    action: LifecycleDispositionAction
    status: Literal["PENDING", "COMMITTED", "ABORTED"]
    recorded_at_ms: int = Field(ge=0)
    updated_by: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)
    state_version: int | None = Field(default=None, ge=1)
    phase: BotLifecyclePhase | None = None
    on_roster: bool | None = None
    active_run_id: str | None = None
    desired_state: DesiredState | None = None
    duty_outcome: BotDutyOutcome | None = None
    admission: LifecycleStartAdmissionEvidence | None = None
    failure: str | None = Field(default=None, max_length=500)


class LifecycleDisposition(BaseModel):
    """The durable explanation returned for a lifecycle command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: LifecycleDispositionReceipt
    lifecycle_state: BotLifecycleStateRecord | None = None
    desired_state: DesiredStateRecord | None = None


class LifecycleStartAdmissionEvidence(BaseModel):
    """The router's typed, attributable proof that Start passed admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: Literal["interactive"]
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    roll_call_offer_id: str | None = Field(default=None, min_length=1, max_length=128)
    admitted_at_ms: int = Field(ge=0)


def stable_bot_lifecycle_disposition_log_path(
    artifacts_root: Path,
    strategy_instance_id: str,
) -> Path:
    safe_strategy_instance_id = safe_strategy_instance_path_segment(strategy_instance_id)
    return (
        strategy_instance_artifact_dir(
            artifacts_root, "live_state", safe_strategy_instance_id
        )
        / "lifecycle_dispositions.jsonl"
    )


class BotLifecycleEvaluator:
    """Serialize and durably explain every duty/control-plane transition."""

    def __init__(self, artifacts_root: Path, strategy_instance_id: str) -> None:
        self._artifacts_root = artifacts_root
        self._strategy_instance_id = safe_strategy_instance_path_segment(strategy_instance_id)
        self._state_repo = BotLifecycleStateRepo(
            stable_bot_lifecycle_state_path(artifacts_root, strategy_instance_id)
        )
        self._desired_state_repo = DesiredStateRepo(
            stable_desired_state_path(artifacts_root, strategy_instance_id),
            trusted_root=artifacts_root / "live_state",
        )
        self._receipt_path = stable_bot_lifecycle_disposition_log_path(
            artifacts_root, strategy_instance_id
        )

    def record_start_accepted(
        self,
        *,
        run_id: str,
        now_ms: int,
        updated_by: str,
        admission: LifecycleStartAdmissionEvidence,
        reason: str = "start_accepted",
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        """Commit a prepared start after the daemon accepted its actuation.

        Legacy callers that have no prepared receipt retain the same atomic
        behavior. Production Start prepares this disposition before issuing a
        daemon command, closing the crash window where a live child existed
        without evaluator-owned duty intent.
        """

        self._validate_start_admission(run_id=run_id, admission=admission)
        with self._operation_fence(operation_fence_held), _file_lock(self._receipt_path):
            self._recover_pending_receipts_locked()
            pending = self._pending_prepared_start_locked(run_id=run_id)
            if pending is not None:
                return self._commit_prepared_start_locked(
                    pending,
                    now_ms=now_ms,
                    updated_by=updated_by,
                    reason=reason,
                )

        return self._record_start_without_prepare(
            run_id=run_id,
            now_ms=now_ms,
            updated_by=updated_by,
            admission=admission,
            reason=reason,
            operation_fence_held=operation_fence_held,
        )

    def prepare_start(
        self,
        *,
        run_id: str,
        now_ms: int,
        updated_by: str,
        admission: LifecycleStartAdmissionEvidence,
        reason: str = "start_actuation_prepared",
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        """Durably prepare a Start before the router asks the daemon to spawn.

        The prepared receipt is intentionally not a duty-state mutation. A
        response-loss recovery can observe the daemon and either commit this
        exact receipt as ``ON_DUTY`` or abort it without guessing that a child
        ran.
        """

        self._validate_start_admission(run_id=run_id, admission=admission)
        with self._operation_fence(operation_fence_held), _file_lock(self._receipt_path):
            self._recover_pending_receipts_locked()
            existing = self._pending_prepared_start_locked(run_id=run_id)
            if existing is not None:
                if existing.admission == admission:
                    return LifecycleDisposition(receipt=existing)
                raise LifecycleTransitionRefusedError("START_PREPARE_CONFLICT")
            if self._unresolved_receipt_locked() is not None:
                raise LifecycleTransitionRefusedError("LIFECYCLE_TRANSITION_UNRESOLVED")
            current = self._state_repo.read()
            if current is not None and current.phase is BotLifecyclePhase.RETIRED:
                raise LifecycleTransitionRefusedError(
                    "a retired bot cannot return to duty without a replacement deploy"
                )
            pending = self._append_pending_locked(
                action=LifecycleDispositionAction.START_ACCEPTED,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                admission=admission,
            )
            return LifecycleDisposition(receipt=pending)

    def abort_prepared_start(
        self,
        *,
        run_id: str,
        failure: str,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        """Close a prepared start only after a known daemon-side rejection."""

        with self._operation_fence(operation_fence_held), _file_lock(self._receipt_path):
            self._recover_pending_receipts_locked()
            pending = self._pending_prepared_start_locked(run_id=run_id)
            if pending is None:
                return None
            receipt = self._append_terminal_locked(pending, status="ABORTED", failure=failure)
            return LifecycleDisposition(receipt=receipt)

    def recover_prepared_start_from_daemon_observation(
        self,
        *,
        run_id: str,
        daemon_state: str,
        observed_at_ms: int,
        updated_by: str = "daemon_observation_recovery",
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        """Resolve a response-loss start only from a fresh daemon observation."""

        with self._operation_fence(operation_fence_held), _file_lock(self._receipt_path):
            self._recover_pending_receipts_locked()
            pending = self._pending_prepared_start_locked(run_id=run_id)
            if pending is None:
                return None
            if daemon_state == "running":
                return self._commit_prepared_start_locked(
                    pending,
                    now_ms=observed_at_ms,
                    updated_by=updated_by,
                    reason="daemon_observed_running_after_start_outcome_unknown",
                )
            if daemon_state in {"idle", "stopped", "exited"}:
                receipt = self._append_terminal_locked(
                    pending,
                    status="ABORTED",
                    failure=f"daemon observed {daemon_state} after prepared start",
                )
                return LifecycleDisposition(receipt=receipt)
            return None

    def _record_start_without_prepare(
        self,
        *,
        run_id: str,
        now_ms: int,
        updated_by: str,
        admission: LifecycleStartAdmissionEvidence,
        reason: str,
        operation_fence_held: bool,
    ) -> LifecycleDisposition:
        """Compatibility implementation for historical direct evaluator callers."""

        with self._operation_fence(operation_fence_held):
            def mutate(receipt_id: str) -> BotLifecycleStateRecord:
                current = self._state_repo.read()
                if current is not None and current.phase is BotLifecyclePhase.RETIRED:
                    raise LifecycleTransitionRefusedError(
                        "a retired bot cannot return to duty without a replacement deploy"
                    )
                return self._state_repo.set_phase(
                    BotLifecyclePhase.ON_DUTY,
                    now_ms=now_ms,
                    updated_by=updated_by,
                    active_run_id=run_id,
                    reason=reason,
                    disposition_id=receipt_id,
                    disposition_action=LifecycleDispositionAction.START_ACCEPTED.value,
                )
            return self._record_lifecycle(
                action=LifecycleDispositionAction.START_ACCEPTED,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                admission=admission,
                mutate=mutate,
            )

    def _validate_start_admission(
        self,
        *,
        run_id: str,
        admission: LifecycleStartAdmissionEvidence,
    ) -> None:
        if admission.strategy_instance_id != self._strategy_instance_id or admission.run_id != run_id:
            raise LifecycleTransitionRefusedError(
                "START_ACCEPTED requires matching typed account-admission evidence"
            )

    def _pending_prepared_start_locked(self, *, run_id: str) -> LifecycleDispositionReceipt | None:
        pending = self._unresolved_receipt_locked()
        if pending is None or pending.action is not LifecycleDispositionAction.START_ACCEPTED:
            return None
        if pending.admission is None or pending.admission.run_id != run_id:
            raise LifecycleTransitionRefusedError("START_PREPARE_CONFLICT")
        return pending

    def _unresolved_receipt_locked(self) -> LifecycleDispositionReceipt | None:
        receipts = self._read_receipts_locked()
        terminal_ids = {receipt.receipt_id for receipt in receipts if receipt.status != "PENDING"}
        pending = [
            receipt
            for receipt in receipts
            if receipt.status == "PENDING" and receipt.receipt_id not in terminal_ids
        ]
        if len(pending) > 1:
            raise LifecycleDispositionCorruptError("more than one unresolved lifecycle disposition")
        return pending[0] if pending else None

    def _commit_prepared_start_locked(
        self,
        pending: LifecycleDispositionReceipt,
        *,
        now_ms: int,
        updated_by: str,
        reason: str,
    ) -> LifecycleDisposition:
        current = self._state_repo.read()
        if current is not None and current.phase is BotLifecyclePhase.RETIRED:
            self._append_terminal_locked(
                pending,
                status="ABORTED",
                failure="a retired bot cannot return to duty without a replacement deploy",
            )
            raise LifecycleTransitionRefusedError(
                "a retired bot cannot return to duty without a replacement deploy"
            )
        state = self._state_repo.set_phase(
            BotLifecyclePhase.ON_DUTY,
            now_ms=now_ms,
            updated_by=updated_by,
            active_run_id=pending.admission.run_id if pending.admission is not None else None,
            reason=reason,
            disposition_id=pending.receipt_id,
            disposition_action=LifecycleDispositionAction.START_ACCEPTED.value,
        )
        receipt = self._append_terminal_locked(pending, status="COMMITTED", lifecycle_state=state)
        return LifecycleDisposition(receipt=receipt, lifecycle_state=state)

    def set_roster(
        self,
        on_roster: bool,
        *,
        now_ms: int,
        updated_by: str,
        reason: str | None = None,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        with self._operation_fence(operation_fence_held):
            def mutate(receipt_id: str) -> BotLifecycleStateRecord:
                current = self._state_repo.read()
                if current is not None and current.phase is BotLifecyclePhase.RETIRED:
                    raise LifecycleTransitionRefusedError("a retired bot cannot be added to the duty roster")
                return self._state_repo.set_roster(
                    on_roster,
                    now_ms=now_ms,
                    updated_by=updated_by,
                    reason=reason,
                    disposition_id=receipt_id,
                    disposition_action=LifecycleDispositionAction.ROSTER_CHANGED.value,
                )
            return self._record_lifecycle(
                action=LifecycleDispositionAction.ROSTER_CHANGED,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                mutate=mutate,
            )

    def record_terminal_outcome(
        self,
        outcome: BotDutyOutcome,
        *,
        updated_by: str,
        reason: str,
        expected_active_run_id: str | None = None,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        """Fold a daemon-reported process outcome through the duty machine."""

        with self._operation_fence(operation_fence_held):
            self._recover_pending_receipts()
            current = self._state_repo.read()
            if current is not None and current.phase is BotLifecyclePhase.RETIRED:
                return None

            def mutate(receipt_id: str) -> BotLifecycleStateRecord:
                return self._state_repo.record_terminal_outcome(
                    outcome,
                    updated_by=updated_by,
                    reason=reason,
                    expected_active_run_id=expected_active_run_id,
                    disposition_id=receipt_id,
                    disposition_action=LifecycleDispositionAction.TERMINAL_OUTCOME.value,
                )
            return self._record_lifecycle(
                action=LifecycleDispositionAction.TERMINAL_OUTCOME,
                now_ms=outcome.recorded_at_ms,
                updated_by=updated_by,
                reason=reason,
                mutate=mutate,
            )

    def retire(
        self,
        *,
        now_ms: int,
        updated_by: str,
        reason: str,
        replacement_strategy_instance_id: str | None = None,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        with self._operation_fence(operation_fence_held):
            self._recover_pending_receipts()
            current = self._state_repo.read()
            if current is not None and current.phase is BotLifecyclePhase.RETIRED:
                return self._existing_lifecycle_disposition(
                    action=LifecycleDispositionAction.RETIRED,
                    now_ms=now_ms,
                    updated_by=updated_by,
                    reason=reason,
                    record=current,
                )

            def mutate(receipt_id: str) -> BotLifecycleStateRecord:
                return self._state_repo.retire(
                    now_ms=now_ms,
                    updated_by=updated_by,
                    reason=reason,
                    replacement_strategy_instance_id=replacement_strategy_instance_id,
                    disposition_id=receipt_id,
                    disposition_action=LifecycleDispositionAction.RETIRED.value,
                )
            return self._record_lifecycle(
                action=LifecycleDispositionAction.RETIRED,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                mutate=mutate,
            )

    def reopen_for_deploy_if_retired(
        self,
        *,
        now_ms: int,
        updated_by: str,
        reason: str,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        with self._operation_fence(operation_fence_held):
            self._recover_pending_receipts()
            current = self._state_repo.read()
            if current is None or current.phase is not BotLifecyclePhase.RETIRED:
                return None

            def mutate(receipt_id: str) -> BotLifecycleStateRecord:
                return self._state_repo.reopen_for_deploy(
                    now_ms=now_ms,
                    updated_by=updated_by,
                    reason=reason,
                    disposition_id=receipt_id,
                    disposition_action=LifecycleDispositionAction.REOPENED_FOR_DEPLOY.value,
                )
            return self._record_lifecycle(
                action=LifecycleDispositionAction.REOPENED_FOR_DEPLOY,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                mutate=mutate,
            )

    def set_desired_state(
        self,
        state: DesiredState,
        *,
        now_ms: int,
        updated_by: str,
        reason: str | None = None,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        with self._operation_fence(operation_fence_held):
            return self._record_desired_state(
                action=LifecycleDispositionAction.DESIRED_STATE_SET,
                state=state,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                only_if_absent=False,
            )

    def seed_default_desired_state_if_absent(
        self,
        *,
        now_ms: int,
        updated_by: str,
        reason: str,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        """Persist the explicit RUNNING default without overwriting an operator."""

        with self._operation_fence(operation_fence_held):
            self._recover_pending_receipts()
            if self._desired_state_repo.read() is not None:
                return None
            return self._record_desired_state(
                action=LifecycleDispositionAction.DEFAULT_DESIRED_STATE_SEEDED,
                state=DesiredState.RUNNING,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                only_if_absent=True,
            )

    def assert_start_latch_allows_start(self) -> DesiredStateRecord | None:
        """Return the durable start latch without letting Start clear STOPPED."""

        with self._operation_fence():
            self._recover_pending_receipts()
            record = self._desired_state_repo.read()
            if record is not None and record.desired_state is DesiredState.STOPPED:
                raise LifecycleTransitionRefusedError("STOPPED_REQUIRES_RESUME")
            return record

    def _record_desired_state(
        self,
        *,
        action: LifecycleDispositionAction,
        state: DesiredState,
        now_ms: int,
        updated_by: str,
        reason: str | None,
        only_if_absent: bool,
    ) -> LifecycleDisposition | None:
        with _file_lock(self._receipt_path):
            self._recover_pending_receipts_locked()
            if self._unresolved_receipt_locked() is not None:
                raise LifecycleTransitionRefusedError("START_ACTUATION_UNRESOLVED")
            existing = self._desired_state_repo.read()
            if only_if_absent and existing is not None:
                return None
            pending = self._append_pending_locked(
                action=action,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
            )
            try:
                record = self._desired_state_repo.set(
                    state,
                    updated_by=updated_by,
                    reason=reason,
                    now_ms=now_ms,
                    disposition_id=pending.receipt_id,
                    disposition_action=action.value,
                )
            except BaseException as exc:
                self._append_terminal_locked(pending, status="ABORTED", failure=str(exc))
                raise
            receipt = self._append_terminal_locked(
                pending,
                status="COMMITTED",
                desired_state=record,
            )
            return LifecycleDisposition(receipt=receipt, desired_state=record)

    def _record_lifecycle(
        self,
        *,
        action: LifecycleDispositionAction,
        now_ms: int,
        updated_by: str,
        reason: str | None,
        mutate: Callable[[str], BotLifecycleStateRecord],
        admission: LifecycleStartAdmissionEvidence | None = None,
    ) -> LifecycleDisposition:
        with _file_lock(self._receipt_path):
            self._recover_pending_receipts_locked()
            if self._unresolved_receipt_locked() is not None:
                raise LifecycleTransitionRefusedError("START_ACTUATION_UNRESOLVED")
            pending = self._append_pending_locked(
                action=action,
                now_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                admission=admission,
            )
            try:
                record = mutate(pending.receipt_id)
            except BaseException as exc:
                self._append_terminal_locked(pending, status="ABORTED", failure=str(exc))
                raise
            receipt = self._append_terminal_locked(pending, status="COMMITTED", lifecycle_state=record)
            return LifecycleDisposition(receipt=receipt, lifecycle_state=record)

    def _existing_lifecycle_disposition(
        self,
        *,
        action: LifecycleDispositionAction,
        now_ms: int,
        updated_by: str,
        reason: str,
        record: BotLifecycleStateRecord,
    ) -> LifecycleDisposition:
        """Return a durable-state receipt for an idempotent already-retired command."""

        receipt = LifecycleDispositionReceipt(
            sequence=record.version,
            receipt_id=record.last_disposition_id or f"{self._strategy_instance_id}:state:{record.version}",
            strategy_instance_id=self._strategy_instance_id,
            action=action,
            status="COMMITTED",
            recorded_at_ms=now_ms,
            updated_by=updated_by,
            reason=reason,
            state_version=record.version,
            phase=record.phase,
            on_roster=record.on_roster,
            active_run_id=record.active_run_id,
            duty_outcome=record.duty_outcome,
        )
        return LifecycleDisposition(receipt=receipt, lifecycle_state=record)

    def _append_pending_locked(
        self,
        *,
        action: LifecycleDispositionAction,
        now_ms: int,
        updated_by: str,
        reason: str | None,
        admission: LifecycleStartAdmissionEvidence | None = None,
    ) -> LifecycleDispositionReceipt:
        sequence = self._next_sequence_locked()
        pending = LifecycleDispositionReceipt(
            sequence=sequence,
            receipt_id=f"{self._strategy_instance_id}:{sequence}",
            strategy_instance_id=self._strategy_instance_id,
            action=action,
            status="PENDING",
            recorded_at_ms=now_ms,
            updated_by=updated_by,
            reason=reason,
            admission=admission,
        )
        append_jsonl_record(
            self._receipt_path,
            pending.model_dump_json(),
            trusted_root=self._artifacts_root / "live_state",
        )
        return pending

    def _append_terminal_locked(
        self,
        pending: LifecycleDispositionReceipt,
        *,
        status: Literal["COMMITTED", "ABORTED"],
        lifecycle_state: BotLifecycleStateRecord | None = None,
        desired_state: DesiredStateRecord | None = None,
        failure: str | None = None,
    ) -> LifecycleDispositionReceipt:
        receipt = pending.model_copy(
            update={
                "status": status,
                "state_version": (
                    lifecycle_state.version
                    if lifecycle_state is not None
                    else (desired_state.version if desired_state is not None else None)
                ),
                "phase": lifecycle_state.phase if lifecycle_state is not None else None,
                "on_roster": lifecycle_state.on_roster if lifecycle_state is not None else None,
                "active_run_id": lifecycle_state.active_run_id if lifecycle_state is not None else None,
                "desired_state": desired_state.desired_state if desired_state is not None else None,
                "duty_outcome": lifecycle_state.duty_outcome if lifecycle_state is not None else None,
                "failure": failure,
            }
        )
        append_jsonl_record(
            self._receipt_path,
            receipt.model_dump_json(),
            trusted_root=self._artifacts_root / "live_state",
        )
        return receipt

    def _recover_pending_receipts_locked(self) -> None:
        receipts = self._read_receipts_locked()
        terminal_ids = {receipt.receipt_id for receipt in receipts if receipt.status != "PENDING"}
        pending = [receipt for receipt in receipts if receipt.status == "PENDING" and receipt.receipt_id not in terminal_ids]
        if len(pending) > 1:
            raise LifecycleDispositionCorruptError("more than one unresolved lifecycle disposition")
        if not pending:
            return
        receipt = pending[0]
        if receipt.action is LifecycleDispositionAction.START_ACCEPTED:
            # A Start's PENDING receipt is a deliberate write-ahead intent.
            # It can be resolved only by a known daemon rejection or a fresh
            # daemon observation, never by the absence of an atomic state
            # witness immediately after a response-loss crash.
            return
        lifecycle = self._state_repo.read()
        if lifecycle is not None and lifecycle.last_disposition_id == receipt.receipt_id:
            self._append_terminal_locked(receipt, status="COMMITTED", lifecycle_state=lifecycle)
            return
        desired_state = self._desired_state_repo.read()
        if desired_state is not None and desired_state.last_disposition_id == receipt.receipt_id:
            self._append_terminal_locked(receipt, status="COMMITTED", desired_state=desired_state)
        else:
            self._append_terminal_locked(
                receipt,
                status="ABORTED",
                failure="state transition was not durably observed during evaluator recovery",
            )

    def _recover_pending_receipts(self) -> None:
        with _file_lock(self._receipt_path):
            self._recover_pending_receipts_locked()

    def _operation_fence(self, operation_fence_held: bool = False):
        if operation_fence_held:
            return contextlib.nullcontext()
        return bot_lifecycle_operation_fence(self._artifacts_root, self._strategy_instance_id)

    def _next_sequence_locked(self) -> int:
        receipts = self._read_receipts_locked()
        return max((receipt.sequence for receipt in receipts), default=0) + 1

    def _read_receipts_locked(self) -> tuple[LifecycleDispositionReceipt, ...]:
        if not self._receipt_path.exists():
            return ()
        try:
            lines = self._receipt_path.read_text(encoding="utf-8").splitlines()
            receipts = tuple(LifecycleDispositionReceipt.model_validate_json(line) for line in lines)
        except (OSError, ValidationError, ValueError) as exc:
            raise LifecycleDispositionCorruptError(
                f"lifecycle disposition log at {self._receipt_path} is unreadable: {exc}"
            ) from exc
        pending_by_id: dict[str, LifecycleDispositionReceipt] = {}
        completed_ids: set[str] = set()
        expected_sequence = 1
        unresolved_receipt_id: str | None = None
        for receipt in receipts:
            if receipt.strategy_instance_id != self._strategy_instance_id:
                raise LifecycleDispositionCorruptError("lifecycle disposition identity does not match its path")
            if receipt.status == "PENDING":
                if unresolved_receipt_id is not None:
                    raise LifecycleDispositionCorruptError(
                        "lifecycle disposition prepare arrived before the prior disposition completed"
                    )
                if receipt.sequence != expected_sequence:
                    raise LifecycleDispositionCorruptError("lifecycle disposition sequence is not contiguous")
                if receipt.receipt_id in pending_by_id:
                    raise LifecycleDispositionCorruptError("duplicate lifecycle disposition prepare")
                if any(
                    value is not None
                    for value in (
                        receipt.state_version,
                        receipt.phase,
                        receipt.on_roster,
                        receipt.active_run_id,
                        receipt.desired_state,
                        receipt.duty_outcome,
                        receipt.failure,
                    )
                ):
                    raise LifecycleDispositionCorruptError("prepared lifecycle disposition contains a result")
                pending_by_id[receipt.receipt_id] = receipt
                unresolved_receipt_id = receipt.receipt_id
                expected_sequence += 1
                continue
            pending = pending_by_id.get(receipt.receipt_id)
            if pending is None:
                raise LifecycleDispositionCorruptError("lifecycle disposition completion has no prepare")
            if receipt.receipt_id != unresolved_receipt_id:
                raise LifecycleDispositionCorruptError("lifecycle disposition completion is out of order")
            if receipt.receipt_id in completed_ids:
                raise LifecycleDispositionCorruptError("duplicate lifecycle disposition completion")
            for field in (
                "schema_version",
                "sequence",
                "strategy_instance_id",
                "action",
                "recorded_at_ms",
                "updated_by",
                "reason",
                "admission",
            ):
                if getattr(receipt, field) != getattr(pending, field):
                    raise LifecycleDispositionCorruptError(
                        "lifecycle disposition completion does not match its prepare"
                    )
            if receipt.status == "COMMITTED":
                lifecycle_result = receipt.phase is not None or receipt.on_roster is not None
                desired_result = receipt.desired_state is not None
                if (
                    receipt.failure is not None
                    or receipt.state_version is None
                    or lifecycle_result == desired_result
                    or (lifecycle_result and receipt.phase is None)
                    or (lifecycle_result and receipt.on_roster is None)
                ):
                    raise LifecycleDispositionCorruptError("invalid committed lifecycle disposition")
            elif (
                receipt.failure is None
                or any(
                    value is not None
                    for value in (
                        receipt.state_version,
                        receipt.phase,
                        receipt.on_roster,
                        receipt.active_run_id,
                        receipt.desired_state,
                        receipt.duty_outcome,
                    )
                )
            ):
                raise LifecycleDispositionCorruptError("invalid aborted lifecycle disposition")
            completed_ids.add(receipt.receipt_id)
            unresolved_receipt_id = None
        unresolved = set(pending_by_id) - completed_ids
        if len(unresolved) > 1:
            raise LifecycleDispositionCorruptError("more than one unresolved lifecycle disposition")
        return receipts


__all__ = [
    "BotLifecycleEvaluator",
    "LifecycleDisposition",
    "LifecycleDispositionAction",
    "LifecycleDispositionCorruptError",
    "LifecycleDispositionReceipt",
    "LifecycleStartAdmissionEvidence",
    "LifecycleTransitionRefusedError",
    "stable_bot_lifecycle_disposition_log_path",
]
