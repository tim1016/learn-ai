"""Dispatch and proof boundary for signed account recovery actions.

The account router verifies HTTP envelopes. This module owns the recovery
state-machine branches: it derives no browser input, sends effects only
through Clerk-owned lanes, and requires fresh reconciliation before claiming
that a cancellation or flatten completed.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.account_clerk_journal import read_account_clerk_durability_spine
from app.engine.live.account_clerk_journal_models import AccountClerkEmergencyAuthorization
from app.engine.live.account_effect_models import AccountEffectPurpose, AccountEffectRequest
from app.engine.live.account_owner import (
    MANUAL_OPERATOR_RUN_ID,
    MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
    AccountOwnerSubmitIntent,
)
from app.engine.live.order_identity import build_manual_order_namespace, build_order_ref
from app.schemas.account_reconciliation import AccountReconciliationReceipt
from app.schemas.account_safety_snapshot import (
    PresentedOperatorAction,
    PresentedOperatorActionEffectReceipt,
)
from app.schemas.journal_cures import (
    AccountRecoveryFlattenCandidate,
    OperatorExactCancelRequest,
    OperatorExactCancelResponse,
    OperatorPendingCancelResponse,
    OperatorRecoveryFlattenResponse,
)
from app.schemas.live_runs import AccountEmergencyFlattenDispatchRequest, AccountEmergencyFlattenResponse
from app.schemas.presented_operator_action import PresentedOperatorActionTarget
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_safety_snapshot import AccountSafetySnapshotService
from app.services.presented_account_actions import PresentedActionRejectedError
from app.services.presented_recovery_actions import PresentedRecoveryActionOutcome
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)
_ExpectedProof = Literal["ORDER_ABSENT", "POSITION_FLAT", "ACCOUNT_FLAT"]
_ReconcileAfterEffect = Callable[[], Awaitable[AccountReconciliationReceipt]]
_PROVEN_NO_EFFECT_CLERK_REJECTION_CODES = frozenset({"CLERK_A0_CANCEL_TARGET_NOT_PENDING"})


async def dispatch_presented_recovery_action(
    *,
    action: PresentedOperatorAction,
    account_id: str,
    reconciliation: AccountReconciliationService,
    snapshot_service: AccountSafetySnapshotService,
    artifacts_root: Path,
    reconcile_after_effect: _ReconcileAfterEffect,
) -> PresentedRecoveryActionOutcome:
    """Dispatch one validated action and preserve known Clerk refusals."""

    try:
        return await _dispatch_recovery_action(
            action=action,
            account_id=account_id,
            reconciliation=reconciliation,
            snapshot_service=snapshot_service,
            artifacts_root=artifacts_root,
            reconcile_after_effect=reconcile_after_effect,
        )
    except host_daemon_client.HostDaemonError as exc:
        rejection = known_host_recovery_rejection(exc)
        if rejection is not None:
            raise rejection from exc
        raise


def known_host_recovery_rejection(
    exc: host_daemon_client.HostDaemonError,
) -> PresentedActionRejectedError | None:
    """Keep explicit Clerk refusals distinct from uncertain broker effects."""

    if exc.status_code != 409:
        return None
    detail = exc.detail
    if not isinstance(detail, dict):
        return None
    reason_code = detail.get("reason_code")
    message = detail.get("message")
    if not isinstance(reason_code, str) or not isinstance(message, str):
        return None
    if reason_code not in _PROVEN_NO_EFFECT_CLERK_REJECTION_CODES:
        return None
    return PresentedActionRejectedError(reason_code, message)


async def _dispatch_recovery_action(
    *,
    action: PresentedOperatorAction,
    account_id: str,
    reconciliation: AccountReconciliationService,
    snapshot_service: AccountSafetySnapshotService,
    artifacts_root: Path,
    reconcile_after_effect: _ReconcileAfterEffect,
) -> PresentedRecoveryActionOutcome:
    """Dispatch one already-validated action without deriving browser input."""

    target = action.target
    if action.action_id == "cancel_pending" and target.kind == "A0_INTENT":
        intent = _current_a0_intent(artifacts_root=artifacts_root, account_id=account_id, action=action)
        settings = get_settings()
        payload = await host_daemon_client.operator_pending_cancel(
            settings.live_runner_daemon_url,
            account_id,
            OperatorExactCancelRequest(intent=intent).model_dump(mode="json"),
        )
        receipt = OperatorPendingCancelResponse.model_validate(payload).cancelled_before_submit
        return PresentedRecoveryActionOutcome(
            state="ACCEPTED",
            finished_copy="Pending custody cancel accepted. No broker order was sent.",
            effect_receipt=PresentedOperatorActionEffectReceipt(
                kind="A0_CANCELLED",
                account_id=account_id,
                intent_id=receipt.recorded.intent_id,
                order_ref=receipt.recorded.order_ref,
                clerk_journal_seq=receipt.cancelled_journal_seq,
                recorded_at_ms=receipt.cancelled_at_ms,
            ),
            refreshed_snapshot_id=snapshot_service.snapshot(account_id=account_id).snapshot_id,
        )
    if action.action_id == "cancel_exact" and target.kind == "EXACT_ORDER":
        intent = _exact_cancel_intent(action)
        settings = get_settings()
        payload = await host_daemon_client.operator_exact_cancel(
            settings.live_runner_daemon_url,
            account_id,
            OperatorExactCancelRequest(intent=intent).model_dump(mode="json"),
        )
        receipt = OperatorExactCancelResponse.model_validate(payload).cancel_confirmed
        effect = PresentedOperatorActionEffectReceipt(
            kind="EXACT_CANCEL_CONFIRMED",
            account_id=account_id,
            intent_id=receipt.recorded.intent_id,
            order_ref=receipt.recorded.order_ref,
            clerk_journal_seq=receipt.recorded.journal_seq,
            recorded_at_ms=receipt.recorded.recorded_at_ms,
        )
        return await _post_effect_reconciliation(
            account_id=account_id,
            snapshot_service=snapshot_service,
            effect=effect,
            target=target,
            expected="ORDER_ABSENT",
            reconcile_after_effect=reconcile_after_effect,
        )
    if action.action_id == "flatten" and target.kind == "RECOVERY_NAMESPACE":
        candidate = _current_recovery_candidate(
            reconciliation=reconciliation,
            account_id=account_id,
            action=action,
        )
        settings = get_settings()
        payload = await host_daemon_client.operator_recovery_flatten(
            settings.live_runner_daemon_url,
            account_id,
            {
                "intent": candidate.intent.model_dump(mode="json"),
                "request_provenance": f"presented-action:{action.idempotency_key}",
            },
        )
        receipt = OperatorRecoveryFlattenResponse.model_validate(payload).recovery_flatten
        effect = PresentedOperatorActionEffectReceipt(
            kind="RECOVERY_FLATTEN_SUBMITTED",
            account_id=account_id,
            intent_id=receipt.recorded.intent_id,
            order_ref=receipt.recorded.order_ref,
            clerk_journal_seq=receipt.broker_acked.journal_seq,
            recorded_at_ms=receipt.broker_acked.recorded_at_ms,
        )
        return await _post_effect_reconciliation(
            account_id=account_id,
            snapshot_service=snapshot_service,
            effect=effect,
            target=target,
            expected="POSITION_FLAT",
            reconcile_after_effect=reconcile_after_effect,
        )
    if action.action_id == "flatten" and target.kind == "ACCOUNT_EMERGENCY":
        receipt = _current_emergency_flatten_receipt(
            reconciliation=reconciliation,
            account_id=account_id,
        )
        settings = get_settings()
        authorization_payload = await host_daemon_client.authorize_emergency_flatten(
            settings.live_runner_daemon_url,
            account_id,
            {
                "operation_id": action.idempotency_key,
                "reconciliation_evidence_version": receipt.receipt_id,
            },
        )
        authorization = AccountClerkEmergencyAuthorization.model_validate(authorization_payload)
        payload = await host_daemon_client.emergency_flatten_account(
            settings.live_runner_daemon_url,
            account_id,
            AccountEmergencyFlattenDispatchRequest(
                account=account_id,
                confirmation_token="FLATTEN",
                idempotency_key=action.idempotency_key,
                authorization_id=authorization.authorization_id,
            ).model_dump(mode="json"),
        )
        emergency = AccountEmergencyFlattenResponse.model_validate(payload)
        effect = PresentedOperatorActionEffectReceipt(
            kind="ACCOUNT_FLATTEN_OBSERVED",
            account_id=account_id,
            operation_id=action.idempotency_key,
            recorded_at_ms=emergency.observed_at_ms,
        )
        return await _post_effect_reconciliation(
            account_id=account_id,
            snapshot_service=snapshot_service,
            effect=effect,
            target=target,
            expected="ACCOUNT_FLAT",
            reconcile_after_effect=reconcile_after_effect,
        )
    if action.action_id == "flatten" and target.kind == "ACCOUNT":
        return PresentedRecoveryActionOutcome(
            state="PENDING_PROOF",
            finished_copy=(
                "Flatten intention recorded. Reconcile fresh broker evidence before any liquidation can be submitted."
            ),
            effect_receipt=PresentedOperatorActionEffectReceipt(
                kind="FLATTEN_INTENTION_RECORDED",
                account_id=account_id,
                recorded_at_ms=now_ms_utc(),
            ),
            refreshed_snapshot_id=snapshot_service.snapshot(account_id=account_id).snapshot_id,
        )
    raise PresentedActionRejectedError(
        "ACTION_TARGET_MISMATCH", "The signed action target is not valid for this recovery operation."
    )


def _current_a0_intent(
    *,
    artifacts_root: Path,
    account_id: str,
    action: PresentedOperatorAction,
) -> AccountOwnerSubmitIntent:
    target = action.target
    entries = read_account_clerk_durability_spine(artifacts_root, account_id)
    matching = [
        entry.intent
        for entry in entries
        if entry.entry_kind == "recorded"
        and entry.intent is not None
        and entry.intent.intent_id == target.target_intent_id
        and entry.intent.order_ref == target.target_intent_order_ref
        and entry.intent.strategy_instance_id == target.strategy_instance_id
        and entry.intent.run_id == target.run_id
    ]
    if len(matching) != 1:
        raise PresentedActionRejectedError(
            "A0_CANCEL_TARGET_NOT_CURRENT", "This pending custody target is no longer current."
        )
    return matching[0]


def _exact_cancel_intent(action: PresentedOperatorAction) -> AccountOwnerSubmitIntent:
    target = action.target
    assert target.target_order_id is not None
    assert target.target_order_ref is not None
    intent_id = f"pcancel-{hashlib.sha256(action.idempotency_key.encode()).hexdigest()[:32]}"
    namespace = build_manual_order_namespace("operator")
    return AccountOwnerSubmitIntent(
        trace_id=f"presented-exact-cancel:{action.idempotency_key}",
        account_id=target.account_id,
        strategy_instance_id=MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
        run_id=MANUAL_OPERATOR_RUN_ID,
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=build_order_ref(namespace, intent_id),
        intent_kind="EXACT_CANCEL",
        order_spec={},
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CANCEL,
            target_order_id=target.target_order_id,
            target_order_ref=target.target_order_ref,
        ),
        owner_generation=0,
        created_at_ms=action.issued_at_ms,
    )


def _current_recovery_candidate(
    *,
    reconciliation: AccountReconciliationService,
    account_id: str,
    action: PresentedOperatorAction,
) -> AccountRecoveryFlattenCandidate:
    target = action.target
    triage = reconciliation.triage(account_id=account_id, now_ms=now_ms_utc())
    for candidate in triage.recovery_flatten_candidates:
        try:
            spec = candidate.intent.order_spec
            candidate_con_id = spec.get("con_id")
            candidate_quantity = float(spec["quantity"])
            candidate_signed_quantity = candidate_quantity if spec["action"] == "SELL" else -candidate_quantity
        except (KeyError, TypeError, ValueError):
            continue
        if (
            candidate.intent.intent_id == target.recovery_intent_id
            and candidate.intent.order_ref == target.recovery_order_ref
            and candidate.intent.strategy_instance_id == target.strategy_instance_id
            and candidate.intent.run_id == target.run_id
            and candidate_con_id == target.target_con_id
            and candidate_signed_quantity == target.expected_signed_quantity
        ):
            return candidate
    raise PresentedActionRejectedError(
        "RECOVERY_FLATTEN_TARGET_NOT_CURRENT",
        "Fresh reconciliation no longer proves this exact recovery target safe.",
    )


def _current_emergency_flatten_receipt(
    *,
    reconciliation: AccountReconciliationService,
    account_id: str,
) -> AccountReconciliationReceipt:
    triage = reconciliation.triage(account_id=account_id, now_ms=now_ms_utc())
    receipt = triage.account_reconciliation_receipt
    if triage.emergency_flatten_confirmation is None or receipt is None or triage.recovery_flatten_candidates:
        raise PresentedActionRejectedError(
            "ACCOUNT_EMERGENCY_FLATTEN_NOT_CURRENT",
            "Fresh account evidence no longer declares emergency flatten safe.",
        )
    return receipt


async def _post_effect_reconciliation(
    *,
    account_id: str,
    snapshot_service: AccountSafetySnapshotService,
    effect: PresentedOperatorActionEffectReceipt,
    target: PresentedOperatorActionTarget,
    expected: _ExpectedProof,
    reconcile_after_effect: _ReconcileAfterEffect,
) -> PresentedRecoveryActionOutcome:
    """Never turn a Clerk broker receipt into an unobserved flat claim."""

    try:
        receipt = await reconcile_after_effect()
    except Exception as exc:  # The broker effect is known; only the proof is pending.
        logger.warning(
            "post-action reconciliation proof unavailable for %s after %s: %s",
            account_id,
            effect.kind,
            exc,
        )
        return PresentedRecoveryActionOutcome(
            state="PENDING_PROOF",
            finished_copy=(
                "The Clerk recorded the action, but fresh post-action broker proof is unavailable. "
                "Reconcile before treating the account as flat."
            ),
            effect_receipt=effect,
        )
    observed = _post_effect_proven(receipt=receipt, target=target, expected=expected)
    return PresentedRecoveryActionOutcome(
        state="ACCEPTED" if observed else "PENDING_PROOF",
        finished_copy=(
            "Fresh reconciliation proves the requested broker effect."
            if observed
            else "The Clerk action is recorded, but fresh reconciliation does not yet prove the requested effect."
        ),
        effect_receipt=effect,
        reconciliation_receipt=receipt,
        refreshed_snapshot_id=snapshot_service.snapshot(account_id=account_id).snapshot_id,
    )


def _post_effect_proven(
    *,
    receipt: AccountReconciliationReceipt,
    target: PresentedOperatorActionTarget,
    expected: _ExpectedProof,
) -> bool:
    if expected == "ORDER_ABSENT":
        return not any(
            order.fact_kind == "open_order"
            and order.order_id == target.target_order_id
            and order.order_ref == target.target_order_ref
            and order.remaining > 0
            for order in receipt.account_truth.orders
        )
    if expected == "POSITION_FLAT":
        return not any(
            position.con_id == target.target_con_id and position.quantity != 0
            for position in receipt.account_truth.positions
        )
    return not any(
        position.quantity != 0 for position in receipt.account_truth.positions
    ) and not any(
        order.fact_kind == "open_order" and order.remaining > 0 for order in receipt.account_truth.orders
    )
