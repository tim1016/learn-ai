"""Closed presentation and validation for ordinary lifecycle actions.

The safety snapshot owns the account proof.  This module binds that proof to
the exact instance/run the operator selected, then produces a short-lived,
HMAC-protected action envelope.  It deliberately does not send daemon
commands or write lifecycle state; the existing lifecycle writer and mutation
attempt scope remain the only side-effect authorities.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.schemas.account_reconciliation import AccountReconciliationEvidenceRef
from app.schemas.account_safety_snapshot import (
    AccountSafetySnapshot,
    PresentedOperatorAction,
)
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.schemas.presented_operator_action import (
    PresentedOperatorActionInvocation,
    PresentedOperatorActionPrecondition,
    PresentedOperatorActionTarget,
    has_valid_presented_operator_action_token,
    issue_presented_operator_action_token,
    presented_operator_action_signing_available,
)
from app.utils.timestamps import now_ms_utc

LifecyclePresentedActionId = Literal["pause", "stop", "end_day", "resume", "start", "deploy"]

_PRESENTATION_TTL_MS = 60_000
_RISK_REDUCING_ACTIONS: frozenset[LifecyclePresentedActionId] = frozenset({"pause", "stop", "end_day"})

@dataclass(frozen=True)
class LifecycleActionCopy:
    title: str
    body: str
    consequence: str
    confirm_label: str
    finished_copy: str


_COPY: dict[LifecyclePresentedActionId, LifecycleActionCopy] = {
    "pause": LifecycleActionCopy(
        title="Pause this bot",
        body="Record durable PAUSED intent for this bot.",
        consequence="The bot will stop accepting new entries when it next observes the intent.",
        confirm_label="Pause bot",
        finished_copy="Pause intent accepted. Observe Bot Control to confirm the runtime effect.",
    ),
    "stop": LifecycleActionCopy(
        title="Stop this bot",
        body="Record durable STOPPED intent for this bot.",
        consequence="The bot cannot restart until an operator explicitly resumes it.",
        confirm_label="Stop bot",
        finished_copy="Stop intent accepted. Observe Bot Control to confirm the runtime effect.",
    ),
    "end_day": LifecycleActionCopy(
        title="End this bot's day",
        body="Record durable PAUSED intent and request a clean clock-out when the runner is reachable.",
        consequence="No new entries may be opened while the clean exit is pending.",
        confirm_label="End day",
        finished_copy="End-day intent accepted. Observe Bot Control to confirm the runtime effect.",
    ),
    "resume": LifecycleActionCopy(
        title="Resume this bot",
        body="Request RUNNING intent for this bot.",
        consequence="This can permit new trading activity, so current account safety proof is required.",
        confirm_label="Resume bot",
        finished_copy="Resume request accepted. Observe Bot Control to confirm the runtime effect.",
    ),
    "start": LifecycleActionCopy(
        title="Start this bot",
        body="Request the host daemon to start this exact run.",
        consequence="This can create a live trading process, so current account safety proof is required.",
        confirm_label="Start bot",
        finished_copy="Start request accepted. Observe Bot Control to confirm the runtime effect.",
    ),
    "deploy": LifecycleActionCopy(
        title="Deploy this bot",
        body="Request the host daemon to create this bot run.",
        consequence="This can create a live trading process, so current account safety proof is required.",
        confirm_label="Deploy bot",
        finished_copy="Deploy request accepted. Observe Bot Control to confirm the runtime effect.",
    ),
}


class PresentedLifecycleActionRejectedError(ValueError):
    """A lifecycle action cannot cross its presentation boundary."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class PresentedLifecycleActionService:
    """Produce and verify lifecycle envelopes from fresh account evidence."""

    def __init__(self, *, now_ms: Callable[[], int] = now_ms_utc) -> None:
        self._now_ms = now_ms

    def present(
        self,
        *,
        action_id: LifecyclePresentedActionId,
        snapshot: AccountSafetySnapshot,
        target: PresentedOperatorActionTarget,
    ) -> PresentedOperatorAction:
        self._validate_target_shape(action_id=action_id, target=target)
        copy = _COPY[action_id]
        signing_available = presented_operator_action_signing_available()
        risk_reducing = action_id in _RISK_REDUCING_ACTIONS
        safety_proven = snapshot.verdict == "CLEAN"
        available = signing_available and (risk_reducing or safety_proven)
        issued_at_ms = self._now_ms()
        expires_at_ms = issued_at_ms + _PRESENTATION_TTL_MS
        idempotency_key = hashlib.sha256(
            f"lifecycle:{action_id}:{snapshot.snapshot_id}:{target.model_dump_json()}:{secrets.token_hex(16)}".encode()
        ).hexdigest()
        precondition_value = "RISK_REDUCING" if risk_reducing else "CLEAN"
        return PresentedOperatorAction(
            action_id=action_id,
            target=target,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.snapshot_version,
            evidence_refs=snapshot.evidence_refs,
            effect_class=(
                "RISK_REDUCING_LIFECYCLE"
                if risk_reducing
                else "RISK_INCREASING_LIFECYCLE"
            ),
            idempotency_key=idempotency_key,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            presentation_token=(
                issue_presented_operator_action_token(
                    action_id=action_id,
                    target=target,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_version=snapshot.snapshot_version,
                    idempotency_key=idempotency_key,
                    issued_at_ms=issued_at_ms,
                    expires_at_ms=expires_at_ms,
                )
                if signing_available
                else None
            ),
            preconditions=(
                PresentedOperatorActionPrecondition(
                    code="account_safety.verdict",
                    expected_value=precondition_value,
                ),
                PresentedOperatorActionPrecondition(
                    code="account_safety.snapshot_id",
                    expected_value=snapshot.snapshot_id,
                ),
            ),
            confirmation=OperatorConfirmationCopy(
                title=copy.title,
                body=copy.body,
                consequence=copy.consequence,
                confirm_label=copy.confirm_label,
            ),
            availability="AVAILABLE" if available else "UNAVAILABLE",
            disposition="fix_here" if available else "wait",
            finished_copy=copy.finished_copy,
        )

    def validate(
        self,
        *,
        action_id: LifecyclePresentedActionId,
        target: PresentedOperatorActionTarget,
        invocation: PresentedOperatorActionInvocation | None,
        snapshot: AccountSafetySnapshot | None,
    ) -> PresentedOperatorAction:
        """Validate current proof before a lifecycle mutation can begin.

        A missing envelope is refused intentionally.  In particular, this is
        how Start/Deploy/Resume turn an outage into a typed rejection rather
        than a command which may fire after connectivity recovers.
        """

        if invocation is None:
            raise PresentedLifecycleActionRejectedError(
                "ACTION_PRESENTATION_REQUIRED",
                "Refresh account safety evidence and use the presented action before continuing.",
            )
        if invocation.action_id != action_id:
            raise PresentedLifecycleActionRejectedError(
                "ACTION_ID_MISMATCH", "This action identifier is not valid here."
            )
        if invocation.target != target:
            raise PresentedLifecycleActionRejectedError(
                "ACTION_TARGET_MISMATCH", "This action belongs to a different bot or run."
            )
        if not has_valid_presented_operator_action_token(invocation):
            raise PresentedLifecycleActionRejectedError(
                "ACTION_ENVELOPE_MISMATCH",
                "The action envelope does not match the server presentation.",
            )
        if self._now_ms() >= invocation.expires_at_ms:
            raise PresentedLifecycleActionRejectedError(
                "ACTION_EXPIRED", "This action presentation expired; refresh account safety evidence."
            )
        if action_id in _RISK_REDUCING_ACTIONS:
            # A pause/stop token was issued from a real snapshot, but an
            # outage may make the *next* read stale or unavailable. Requiring
            # fresh proof here would turn the safety control into a network
            # dependency. Its short expiry and exact target signature still
            # prevent replay against another bot or a later operator intent.
            return self._validated_action(action_id=action_id, target=target, invocation=invocation)
        if snapshot is None:
            raise PresentedLifecycleActionRejectedError(
                "ACTION_PROOF_UNAVAILABLE",
                "Current account safety proof is unavailable; this action was not queued.",
            )
        self._validate_target_shape(action_id=action_id, target=target)
        if not presented_operator_action_signing_available() or snapshot.verdict != "CLEAN":
            raise PresentedLifecycleActionRejectedError(
                "ACTION_NO_LONGER_AVAILABLE",
                "Current account safety proof does not permit this action.",
            )
        if (
            invocation.snapshot_id != snapshot.snapshot_id
            or invocation.snapshot_version != snapshot.snapshot_version
        ):
            raise PresentedLifecycleActionRejectedError(
                "ACTION_SNAPSHOT_STALE",
                "Account safety evidence changed; refresh before continuing.",
            )
        return self._validated_action(
            action_id=action_id,
            target=target,
            invocation=invocation,
            evidence_refs=snapshot.evidence_refs,
        )

    @staticmethod
    def _validated_action(
        *,
        action_id: LifecyclePresentedActionId,
        target: PresentedOperatorActionTarget,
        invocation: PresentedOperatorActionInvocation,
        evidence_refs: tuple[AccountReconciliationEvidenceRef, ...] = (),
    ) -> PresentedOperatorAction:
        copy = _COPY[action_id]
        return PresentedOperatorAction(
            action_id=action_id,
            target=target,
            snapshot_id=invocation.snapshot_id,
            snapshot_version=invocation.snapshot_version,
            evidence_refs=evidence_refs,
            effect_class=(
                "RISK_REDUCING_LIFECYCLE"
                if action_id in _RISK_REDUCING_ACTIONS
                else "RISK_INCREASING_LIFECYCLE"
            ),
            idempotency_key=invocation.idempotency_key,
            issued_at_ms=invocation.issued_at_ms,
            expires_at_ms=invocation.expires_at_ms,
            presentation_token=invocation.presentation_token,
            confirmation=OperatorConfirmationCopy(
                title=copy.title,
                body=copy.body,
                consequence=copy.consequence,
                confirm_label=copy.confirm_label,
            ),
            availability="AVAILABLE",
            disposition="fix_here",
            finished_copy=copy.finished_copy,
        )

    @staticmethod
    def _validate_target_shape(
        *, action_id: LifecyclePresentedActionId,
        target: PresentedOperatorActionTarget,
    ) -> None:
        if action_id == "deploy":
            if target.strategy_instance_id is None or target.run_id is not None:
                raise PresentedLifecycleActionRejectedError(
                    "ACTION_TARGET_MISMATCH", "Deploy must be bound to one strategy instance."
                )
            return
        if target.strategy_instance_id is None:
            raise PresentedLifecycleActionRejectedError(
                "ACTION_TARGET_MISMATCH", "This action must be bound to one strategy instance."
            )
        if action_id == "start" and target.run_id is None:
            raise PresentedLifecycleActionRejectedError(
                "ACTION_TARGET_MISMATCH", "Start must be bound to one exact run."
            )
