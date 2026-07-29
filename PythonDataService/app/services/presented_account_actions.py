"""One closed, snapshot-bound account-action execution boundary."""

from __future__ import annotations

import hashlib
import json
import logging
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
    PresentedOperatorActionInvocation,
    has_valid_presented_operator_action_token,
)
from app.utils.timestamps import now_ms_utc

_ACTION_ATTEMPTS_DIRECTORY = "presented_operator_actions"
logger = logging.getLogger(__name__)


class PresentedActionRejectedError(ValueError):
    """A browser envelope is stale, mismatched, expired, or unavailable."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class PresentedActionOutcomeUnknownError(RuntimeError):
    """The durable action claim exists, but its external effect is unproven."""

    def __init__(self, result: PresentedOperatorActionResult) -> None:
        super().__init__("The requested action was durably claimed, but its outcome is not proven.")
        self.result = result


class _PresentedActionAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    action_attempt_id: str = Field(min_length=16, max_length=160)
    action_id: Literal["reconcile_now"]
    account_id: str = Field(min_length=1, max_length=64)
    request_sha256: str = Field(min_length=64, max_length=64)
    finished_copy: str = Field(min_length=1, max_length=512)
    state: Literal["PENDING", "ACCEPTED", "OUTCOME_UNKNOWN"]
    created_at_ms: int = Field(ge=0)
    completed_at_ms: int | None = Field(default=None, ge=0)
    reconciliation_receipt: AccountReconciliationReceipt | None = None
    refreshed_snapshot_id: str | None = Field(default=None, min_length=16, max_length=64)


class PresentedAccountActionService:
    """Durably claim one exact action envelope before asynchronous broker work."""

    def __init__(self, *, artifacts_root: Path, now_ms: Callable[[], int] = now_ms_utc) -> None:
        self._artifacts_root = artifacts_root
        self._now_ms = now_ms

    async def execute_reconcile(
        self,
        *,
        action: PresentedOperatorAction,
        invocation: PresentedOperatorActionInvocation,
        invoke: Callable[[], Awaitable[AccountReconciliationReceipt]],
        refreshed_snapshot_id: Callable[[], str],
    ) -> PresentedOperatorActionResult:
        self._validate(action=action, invocation=invocation)
        request_sha256 = _request_sha256(invocation)
        prepared = _PresentedActionAttempt(
            action_attempt_id=invocation.idempotency_key,
            action_id=invocation.action_id,
            account_id=invocation.target.account_id,
            request_sha256=request_sha256,
            finished_copy=action.finished_copy,
            state="PENDING",
            created_at_ms=self._now_ms(),
        )
        path = self._path_for(invocation.target.account_id, invocation.idempotency_key)
        existing = self._claim(path, prepared)
        if existing is not None:
            return self._replay(existing, request_sha256)
        try:
            receipt = await invoke()
        except Exception as exc:
            unknown = prepared.model_copy(
                update={"state": "OUTCOME_UNKNOWN", "completed_at_ms": self._now_ms()}
            )
            atomic_write_pydantic_artifact(path, unknown)
            raise PresentedActionOutcomeUnknownError(
                self._result(unknown, replayed=False)
            ) from exc
        try:
            refreshed_snapshot = refreshed_snapshot_id()
        except Exception as exc:
            logger.warning(
                "reconciliation accepted but post-dispatch snapshot refresh failed for %s: %s",
                invocation.target.account_id,
                exc,
            )
            refreshed_snapshot = None
        completed = prepared.model_copy(
            update={
                "state": "ACCEPTED",
                "completed_at_ms": self._now_ms(),
                "reconciliation_receipt": receipt,
                "refreshed_snapshot_id": refreshed_snapshot,
            }
        )
        atomic_write_pydantic_artifact(path, completed)
        return self._result(completed, replayed=False)

    def replay_if_claimed(
        self,
        invocation: PresentedOperatorActionInvocation,
    ) -> PresentedOperatorActionResult | None:
        """Return the original result for a known action before current-state checks.

        A response can be lost after durable acceptance. Once that happens the
        reconciliation may have changed the current snapshot, but retrying the
        exact old envelope must still reveal its one authoritative receipt and
        must never run the mutation again.
        """

        if not has_valid_presented_operator_action_token(invocation):
            raise PresentedActionRejectedError(
                "ACTION_ENVELOPE_MISMATCH",
                "The action envelope does not match the server presentation.",
            )
        path = self._path_for(invocation.target.account_id, invocation.idempotency_key)
        existing = read_pydantic_artifact(path, _PresentedActionAttempt)
        if existing is None:
            if path.is_file():
                raise PresentedActionRejectedError(
                    "ACTION_ATTEMPT_UNREADABLE",
                    "A prior action attempt is unreadable; reconcile before retrying.",
                )
            return None
        return self._replay(existing, _request_sha256(invocation))

    def _validate(
        self,
        *, action: PresentedOperatorAction,
        invocation: PresentedOperatorActionInvocation,
    ) -> None:
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
            raise PresentedActionRejectedError("ACTION_NO_LONGER_AVAILABLE", "This action is no longer available.")
        if invocation.target != action.target:
            raise PresentedActionRejectedError("ACTION_TARGET_MISMATCH", "This action belongs to a different account.")
        if invocation.action_id != action.action_id:
            raise PresentedActionRejectedError("ACTION_ID_MISMATCH", "This action identifier is not valid here.")
        if (
            invocation.snapshot_id != action.snapshot_id
            or invocation.snapshot_version != action.snapshot_version
            or invocation.idempotency_key != action.idempotency_key
        ):
            raise PresentedActionRejectedError("ACTION_SNAPSHOT_STALE", "Account safety evidence changed; refresh before retrying.")

    def _path_for(self, account_id: str, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return account_artifacts_root(self._artifacts_root, account_id) / _ACTION_ATTEMPTS_DIRECTORY / f"{digest}.json"

    @staticmethod
    def _claim(path: Path, prepared: _PresentedActionAttempt) -> _PresentedActionAttempt | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            durable_append_log.create_exclusive_durable_file(path, prepared.model_dump_json(), trusted_root=path.parent)
        except FileExistsError:
            existing = read_pydantic_artifact(path, _PresentedActionAttempt)
            if existing is None:
                raise PresentedActionRejectedError("ACTION_ATTEMPT_UNREADABLE", "A prior action attempt is unreadable; reconcile before retrying.")
            return existing
        return None

    @staticmethod
    def _replay(
        existing: _PresentedActionAttempt,
        request_sha256: str,
    ) -> PresentedOperatorActionResult:
        if existing.request_sha256 != request_sha256:
            raise PresentedActionRejectedError("IDEMPOTENCY_KEY_REUSED", "This action key is already bound to different evidence.")
        return PresentedAccountActionService._result(existing, replayed=True)

    @staticmethod
    def _result(
        attempt: _PresentedActionAttempt,
        *,
        replayed: bool,
    ) -> PresentedOperatorActionResult:
        in_progress = attempt.state == "PENDING"
        outcome_unknown = attempt.state == "OUTCOME_UNKNOWN"
        return PresentedOperatorActionResult(
            action_id=attempt.action_id,
            action_attempt_id=attempt.action_attempt_id,
            state="IN_PROGRESS" if in_progress else "OUTCOME_UNKNOWN" if outcome_unknown else "ACCEPTED",
            replayed=replayed,
            finished_copy=(
                "The reconciliation request is already in progress. Refresh account safety evidence shortly."
                if in_progress
                else "The reconciliation request was durably claimed, but its observed outcome is not proven. "
                "Refresh account safety evidence before taking another action."
                if outcome_unknown
                else attempt.finished_copy
            ),
            reconciliation_receipt=attempt.reconciliation_receipt,
            refreshed_snapshot_id=attempt.refreshed_snapshot_id,
        )


def _request_sha256(invocation: PresentedOperatorActionInvocation) -> str:
    # Issued/expiry times fence presentation validity, but do not describe the
    # mutation. A refreshed presentation of the same semantic snapshot must
    # replay its one durable action attempt instead of becoming a key collision.
    semantic_request = {
        "action_id": invocation.action_id,
        "target": invocation.target.model_dump(mode="json"),
        "snapshot_id": invocation.snapshot_id,
        "snapshot_version": invocation.snapshot_version,
        "idempotency_key": invocation.idempotency_key,
    }
    return hashlib.sha256(
        json.dumps(semantic_request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
