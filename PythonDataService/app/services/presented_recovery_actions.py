"""Durable execution boundary for snapshot-bound recovery controls.

The data plane owns this action attempt record; it never becomes a second
broker writer.  Each invocation is claimed before any host-local Clerk call,
so a lost browser/daemon response leaves one replayable result rather than an
invitation to repeat a cancel or flatten.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live import durable_append_log
from app.engine.live.account_artifacts import account_artifacts_root
from app.schemas.account_reconciliation import AccountReconciliationReceipt
from app.schemas.account_safety_snapshot import (
    PresentedOperatorAction,
    PresentedOperatorActionResult,
)
from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact
from app.schemas.presented_operator_action import (
    PresentedOperatorActionEffectReceipt,
    PresentedOperatorActionInvocation,
    has_valid_presented_operator_action_token,
)
from app.services.presented_account_actions import (
    PresentedActionOutcomeUnknownError,
    PresentedActionRejectedError,
)
from app.utils.advisory_lock import try_advisory_file_lock
from app.utils.timestamps import now_ms_utc

_ACTION_ATTEMPTS_DIRECTORY = "presented_recovery_actions"
_RecoveryActionId = Literal["cancel_exact", "cancel_pending", "flatten"]


class PresentedRecoveryActionOutcome(BaseModel):
    """Observed result supplied by the router after the Clerk boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ACCEPTED", "PENDING_PROOF"]
    finished_copy: str = Field(min_length=1, max_length=512)
    effect_receipt: PresentedOperatorActionEffectReceipt | None = None
    reconciliation_receipt: AccountReconciliationReceipt | None = None
    refreshed_snapshot_id: str | None = Field(default=None, min_length=16, max_length=64)


class _PresentedRecoveryActionAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    action_attempt_id: str = Field(min_length=16, max_length=160)
    action_id: _RecoveryActionId
    account_id: str = Field(min_length=1, max_length=64)
    request_sha256: str = Field(min_length=64, max_length=64)
    state: Literal["PENDING", "REJECTED", "ACCEPTED", "PENDING_PROOF", "OUTCOME_UNKNOWN"]
    finished_copy: str = Field(min_length=1, max_length=512)
    created_at_ms: int = Field(ge=0)
    completed_at_ms: int | None = Field(default=None, ge=0)
    last_attempt_at_ms: int = Field(ge=0)
    dispatch_attempt_count: int = Field(default=1, ge=1)
    rejection_reason_code: str | None = Field(default=None, min_length=1, max_length=160)
    rejection_message: str | None = Field(default=None, min_length=1, max_length=512)
    effect_receipt: PresentedOperatorActionEffectReceipt | None = None
    reconciliation_receipt: AccountReconciliationReceipt | None = None
    refreshed_snapshot_id: str | None = Field(default=None, min_length=16, max_length=64)


class PresentedRecoveryActionService:
    """Claim one recovery action before any local or Clerk-owned effect."""

    def __init__(self, *, artifacts_root: Path, now_ms: Callable[[], int] = now_ms_utc) -> None:
        self._artifacts_root = artifacts_root
        self._now_ms = now_ms

    async def execute(
        self,
        *,
        action: PresentedOperatorAction,
        invocation: PresentedOperatorActionInvocation,
        invoke: Callable[[], Awaitable[PresentedRecoveryActionOutcome]],
    ) -> PresentedOperatorActionResult:
        self._validate(action=action, invocation=invocation)
        request_sha256 = _request_sha256(invocation)
        started_at_ms = self._now_ms()
        prepared = _PresentedRecoveryActionAttempt(
            action_attempt_id=invocation.idempotency_key,
            action_id=invocation.action_id,
            account_id=invocation.target.account_id,
            request_sha256=request_sha256,
            state="PENDING",
            finished_copy=action.finished_copy,
            created_at_ms=started_at_ms,
            last_attempt_at_ms=started_at_ms,
        )
        path = self._path_for(invocation.target.account_id, invocation.idempotency_key)
        # Hold this account-local lock through the awaited Clerk/host call.
        # A terminal artifact can be replayed after a process failure, while a
        # live owner cannot be mistaken for a timed-out browser request.
        with try_advisory_file_lock(path) as owns_dispatch:
            if not owns_dispatch:
                existing = self._read_existing(path)
                if existing is not None:
                    return self._replay(existing, request_sha256)
                return self._result(prepared, replayed=True)
            existing = self._claim(path, prepared)
            if existing is not None:
                self._validate_replay_request(existing, request_sha256)
                if existing.state != "PENDING":
                    return self._replay(existing, request_sha256)
                prepared = self._resume_after_owner_exit(
                    path=path,
                    existing=existing,
                    request_sha256=request_sha256,
                )
            try:
                outcome = await invoke()
            except PresentedActionRejectedError as exc:
                rejected = prepared.model_copy(
                    update={
                        "state": "REJECTED",
                        "finished_copy": exc.message,
                        "completed_at_ms": self._now_ms(),
                        "rejection_reason_code": exc.reason_code,
                        "rejection_message": exc.message,
                    }
                )
                atomic_write_pydantic_artifact(path, rejected)
                raise
            except Exception as exc:
                unknown = prepared.model_copy(
                    update={"state": "OUTCOME_UNKNOWN", "completed_at_ms": self._now_ms()}
                )
                atomic_write_pydantic_artifact(path, unknown)
                raise PresentedActionOutcomeUnknownError(self._result(unknown, replayed=False)) from exc
            completed = prepared.model_copy(
                update={
                    "state": outcome.state,
                    "finished_copy": outcome.finished_copy,
                    "completed_at_ms": self._now_ms(),
                    "effect_receipt": outcome.effect_receipt,
                    "reconciliation_receipt": outcome.reconciliation_receipt,
                    "refreshed_snapshot_id": outcome.refreshed_snapshot_id,
                }
            )
            atomic_write_pydantic_artifact(path, completed)
            return self._result(completed, replayed=False)

    def replay_if_claimed(
        self,
        invocation: PresentedOperatorActionInvocation,
    ) -> PresentedOperatorActionResult | None:
        if not has_valid_presented_operator_action_token(invocation):
            raise PresentedActionRejectedError(
                "ACTION_ENVELOPE_MISMATCH",
                "The action envelope does not match the server presentation.",
            )
        path = self._path_for(invocation.target.account_id, invocation.idempotency_key)
        existing = read_pydantic_artifact(path, _PresentedRecoveryActionAttempt)
        if existing is None:
            if path.is_file():
                raise PresentedActionRejectedError(
                    "ACTION_ATTEMPT_UNREADABLE",
                    "A prior action attempt is unreadable; reconcile before retrying.",
                )
            return None
        request_sha256 = _request_sha256(invocation)
        self._validate_replay_request(existing, request_sha256)
        if existing.state == "PENDING":
            # A free lock means the prior owner exited before recording an
            # outcome. Let execute() reclaim it under the same lock. A busy
            # lock proves a live owner, regardless of elapsed wall-clock time.
            with try_advisory_file_lock(path) as owns_dispatch:
                if owns_dispatch:
                    return None
        return self._replay(existing, request_sha256)

    def _resume_after_owner_exit(
        self,
        *,
        path: Path,
        existing: _PresentedRecoveryActionAttempt,
        request_sha256: str,
    ) -> _PresentedRecoveryActionAttempt:
        """Resume a claim only after its previous lock owner has exited.

        The action attempt alone cannot prove whether a crashed process reached
        the host. All recovery dispatches therefore use their deterministic
        Clerk/operation identity again: the Clerk returns its existing receipt,
        records cancellation uncertainty, or refuses a now-stale target. It
        never turns an expired browser lease into a duplicate broker order.
        """

        self._validate_replay_request(existing, request_sha256)
        resumed = existing.model_copy(
            update={
                "last_attempt_at_ms": self._now_ms(),
                "dispatch_attempt_count": existing.dispatch_attempt_count + 1,
            }
        )
        atomic_write_pydantic_artifact(path, resumed)
        return resumed

    @staticmethod
    def _read_existing(path: Path) -> _PresentedRecoveryActionAttempt | None:
        existing = read_pydantic_artifact(path, _PresentedRecoveryActionAttempt)
        if existing is None and path.is_file():
            raise PresentedActionRejectedError(
                "ACTION_ATTEMPT_UNREADABLE",
                "A prior action attempt is unreadable; reconcile before retrying.",
            )
        return existing

    def _validate(
        self,
        *,
        action: PresentedOperatorAction,
        invocation: PresentedOperatorActionInvocation,
    ) -> None:
        if action.action_id not in {"cancel_exact", "cancel_pending", "flatten"}:
            raise PresentedActionRejectedError("ACTION_ID_MISMATCH", "This action is not a recovery action.")
        if not has_valid_presented_operator_action_token(invocation):
            raise PresentedActionRejectedError(
                "ACTION_ENVELOPE_MISMATCH",
                "The action envelope does not match the server presentation.",
            )
        if self._now_ms() >= invocation.expires_at_ms:
            raise PresentedActionRejectedError(
                "ACTION_EXPIRED",
                "This action presentation expired; refresh account safety evidence.",
            )
        if action.availability != "AVAILABLE":
            raise PresentedActionRejectedError(
                "ACTION_NO_LONGER_AVAILABLE", "This action is no longer available."
            )
        if invocation.target != action.target:
            raise PresentedActionRejectedError("ACTION_TARGET_MISMATCH", "This action belongs to different evidence.")
        if invocation.action_id != action.action_id:
            raise PresentedActionRejectedError("ACTION_ID_MISMATCH", "This action identifier is not valid here.")
        if (
            invocation.snapshot_id != action.snapshot_id
            or invocation.snapshot_version != action.snapshot_version
            or invocation.idempotency_key != action.idempotency_key
        ):
            raise PresentedActionRejectedError(
                "ACTION_SNAPSHOT_STALE", "Account safety evidence changed; refresh before continuing."
            )
        required_token = action.confirmation.required_token
        if required_token and invocation.confirmation_token != required_token:
            raise PresentedActionRejectedError(
                "ACTION_CONFIRMATION_REQUIRED",
                "Type the required confirmation before submitting this action.",
            )
        if not required_token and invocation.confirmation_token is not None:
            raise PresentedActionRejectedError(
                "ACTION_CONFIRMATION_UNEXPECTED", "This action does not accept a confirmation token."
            )

    def _path_for(self, account_id: str, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return (
            account_artifacts_root(self._artifacts_root, account_id)
            / _ACTION_ATTEMPTS_DIRECTORY
            / f"{digest}.json"
        )

    @staticmethod
    def _claim(
        path: Path,
        prepared: _PresentedRecoveryActionAttempt,
    ) -> _PresentedRecoveryActionAttempt | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            durable_append_log.create_exclusive_durable_file(
                path, prepared.model_dump_json(), trusted_root=path.parent
            )
        except FileExistsError:
            return PresentedRecoveryActionService._read_existing(path)
        return None

    @staticmethod
    def _validate_replay_request(
        existing: _PresentedRecoveryActionAttempt,
        request_sha256: str,
    ) -> None:
        if existing.request_sha256 != request_sha256:
            raise PresentedActionRejectedError(
                "IDEMPOTENCY_KEY_REUSED", "This action key is already bound to different evidence."
            )

    @staticmethod
    def _replay(
        existing: _PresentedRecoveryActionAttempt,
        request_sha256: str,
    ) -> PresentedOperatorActionResult:
        PresentedRecoveryActionService._validate_replay_request(existing, request_sha256)
        if existing.state == "REJECTED":
            raise PresentedActionRejectedError(
                existing.rejection_reason_code or "ACTION_REJECTED",
                existing.rejection_message or "This recovery action is no longer safe to dispatch.",
            )
        return PresentedRecoveryActionService._result(existing, replayed=True)

    @staticmethod
    def _result(
        attempt: _PresentedRecoveryActionAttempt,
        *,
        replayed: bool,
    ) -> PresentedOperatorActionResult:
        state = (
            "IN_PROGRESS"
            if attempt.state == "PENDING"
            else "OUTCOME_UNKNOWN"
            if attempt.state == "OUTCOME_UNKNOWN"
            else attempt.state
        )
        return PresentedOperatorActionResult(
            action_id=attempt.action_id,
            action_attempt_id=attempt.action_attempt_id,
            state=state,
            replayed=replayed,
            finished_copy=(
                "The recovery action is already in progress. Refresh account safety evidence shortly."
                if state == "IN_PROGRESS"
                else "The recovery action was durably claimed, but its observed outcome is not proven. "
                "Refresh account safety evidence before taking another action."
                if state == "OUTCOME_UNKNOWN"
                else attempt.finished_copy
            ),
            effect_receipt=attempt.effect_receipt,
            reconciliation_receipt=attempt.reconciliation_receipt,
            refreshed_snapshot_id=attempt.refreshed_snapshot_id,
        )


def _request_sha256(invocation: PresentedOperatorActionInvocation) -> str:
    semantic_request = {
        "action_id": invocation.action_id,
        "target": invocation.target.model_dump(mode="json"),
        "snapshot_id": invocation.snapshot_id,
        "snapshot_version": invocation.snapshot_version,
        "idempotency_key": invocation.idempotency_key,
        "confirmation_token": invocation.confirmation_token,
    }
    return hashlib.sha256(
        json.dumps(semantic_request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
