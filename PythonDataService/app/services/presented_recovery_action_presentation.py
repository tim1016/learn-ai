"""Pure presentation of snapshot-bound Cancel and Flatten controls.

This module is deliberately broker-free. It turns already durable Account
Truth, custody, and reconciliation facts into signed, short-lived operator
envelopes; the router and Clerk repeat their own current-state checks before
any side effect.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_clerk_journal_models import AccountClerkCustodyStatus
from app.schemas.account_reconciliation import AccountReconciliationEvidenceRef, AccountReconciliationReceipt
from app.schemas.account_safety_snapshot import PresentedOperatorAction
from app.schemas.journal_cures import AccountRecoveryFlattenCandidate
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.schemas.presented_operator_action import (
    PresentedOperatorActionPrecondition,
    PresentedOperatorActionTarget,
    issue_presented_operator_action_token,
    presented_operator_action_signing_available,
)

_PRESENTATION_TTL_MS = 60_000


def present_recovery_actions(
    *,
    account_id: str,
    snapshot_id: str,
    generated_at_ms: int,
    verdict: str,
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...],
    reconciliation_receipt: AccountReconciliationReceipt | None,
    custody_statuses: Iterable[AccountClerkCustodyStatus],
    recovery_candidates: Iterable[AccountRecoveryFlattenCandidate],
    emergency_confirmation: OperatorConfirmationCopy | None,
) -> tuple[PresentedOperatorAction, ...]:
    """Return only closed actions whose identity exists in durable evidence."""

    signing_available = presented_operator_action_signing_available()
    actions: list[PresentedOperatorAction] = []
    actions.extend(
        _present_pending_cancels(
            account_id=account_id,
            snapshot_id=snapshot_id,
            generated_at_ms=generated_at_ms,
            evidence_refs=evidence_refs,
            custody_statuses=custody_statuses,
            signing_available=signing_available,
        )
    )
    actions.extend(
        _present_exact_cancels(
            account_id=account_id,
            snapshot_id=snapshot_id,
            generated_at_ms=generated_at_ms,
            verdict=verdict,
            evidence_refs=evidence_refs,
            receipt=reconciliation_receipt,
            signing_available=signing_available,
        )
    )
    actions.extend(
        _present_exact_recovery_flattens(
            account_id=account_id,
            snapshot_id=snapshot_id,
            generated_at_ms=generated_at_ms,
            evidence_refs=evidence_refs,
            receipt=reconciliation_receipt,
            candidates=recovery_candidates,
            signing_available=signing_available,
        )
    )
    if emergency_confirmation is not None:
        actions.append(
            _action(
                action_id="flatten",
                target=PresentedOperatorActionTarget(account_id=account_id, kind="ACCOUNT_EMERGENCY"),
                snapshot_id=snapshot_id,
                generated_at_ms=generated_at_ms,
                evidence_refs=evidence_refs,
                effect_class="RISK_REDUCING_BROKER",
                confirmation=emergency_confirmation,
                availability="AVAILABLE" if signing_available else "UNAVAILABLE",
                preconditions=_preconditions(
                    snapshot_id=snapshot_id,
                    receipt=reconciliation_receipt,
                    values=(("account_recovery.mode", "ACCOUNT_EMERGENCY"),),
                ),
                finished_copy=(
                    "Emergency flatten was accepted by the Clerk. Fresh reconciliation is required "
                    "before the account is reported flat."
                ),
                signing_available=signing_available,
            )
        )
    elif not actions or not any(action.action_id == "flatten" for action in actions):
        # Missing or stale evidence is not permission to create a quantity.
        # It is still safe to retain the operator's typed intent, which later
        # reconciliation can turn into a fresh, separately confirmed action.
        actions.append(
            _action(
                action_id="flatten",
                target=PresentedOperatorActionTarget(account_id=account_id),
                snapshot_id=snapshot_id,
                generated_at_ms=generated_at_ms,
                evidence_refs=evidence_refs,
                effect_class="RISK_REDUCING_BROKER",
                confirmation=OperatorConfirmationCopy(
                    title="Record flatten intention",
                    body="Record that you want account exposure flattened after current broker proof is rebuilt.",
                    consequence="No liquidation order will be submitted until fresh reconciliation identifies an exact target and you confirm it again.",
                    confirm_label="Record flatten intention",
                    required_token="FLATTEN",
                ),
                availability="AVAILABLE" if signing_available else "UNAVAILABLE",
                preconditions=(
                    PresentedOperatorActionPrecondition(
                        code="account_safety.snapshot_id", expected_value=snapshot_id
                    ),
                ),
                finished_copy=(
                    "Flatten intention recorded. Reconcile fresh broker evidence before any liquidation can be submitted."
                ),
                signing_available=signing_available,
            )
        )
    return tuple(actions)


def recovery_action_semantic_material(
    *,
    custody_statuses: Iterable[AccountClerkCustodyStatus],
    recovery_candidates: Iterable[AccountRecoveryFlattenCandidate],
    emergency_confirmation: OperatorConfirmationCopy | None,
) -> dict[str, object]:
    """Return stable action-driving facts for the snapshot semantic hash."""

    return {
        "queued_a0": [
            {
                "intent_id": status.intent_id,
                "order_ref": status.order_ref,
                "strategy_instance_id": status.recorded.strategy_instance_id,
                "run_id": status.recorded.run_id,
                "journal_seq": status.recorded.journal_seq,
            }
            for status in custody_statuses
            if status.lifecycle_state == "queued"
        ],
        "recovery_candidates": [
            {
                "intent_id": candidate.intent.intent_id,
                "order_ref": candidate.intent.order_ref,
                "strategy_instance_id": candidate.intent.strategy_instance_id,
                "run_id": candidate.intent.run_id,
                "order_spec": candidate.intent.order_spec,
            }
            for candidate in recovery_candidates
        ],
        "account_emergency_declared": emergency_confirmation is not None,
    }


def _present_pending_cancels(
    *,
    account_id: str,
    snapshot_id: str,
    generated_at_ms: int,
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...],
    custody_statuses: Iterable[AccountClerkCustodyStatus],
    signing_available: bool,
) -> tuple[PresentedOperatorAction, ...]:
    actions: list[PresentedOperatorAction] = []
    for status in custody_statuses:
        if status.lifecycle_state != "queued" or status.account_id != account_id:
            continue
        target = PresentedOperatorActionTarget(
            account_id=account_id,
            strategy_instance_id=status.recorded.strategy_instance_id,
            run_id=status.recorded.run_id,
            kind="A0_INTENT",
            target_intent_id=status.intent_id,
            target_intent_order_ref=status.order_ref,
        )
        actions.append(
            _action(
                action_id="cancel_pending",
                target=target,
                snapshot_id=snapshot_id,
                generated_at_ms=generated_at_ms,
                evidence_refs=evidence_refs,
                effect_class="RISK_REDUCING_BROKER",
                confirmation=OperatorConfirmationCopy(
                    title="Cancel pending custody",
                    body="Cancel this exact Clerk-held intent before a broker write begins.",
                    consequence="The intent becomes terminal locally. If it has crossed the broker boundary, refresh evidence and use exact broker cancellation instead.",
                    confirm_label="Cancel pending intent",
                ),
                availability="AVAILABLE" if signing_available else "UNAVAILABLE",
                preconditions=(
                    PresentedOperatorActionPrecondition(
                        code="custody.stage", expected_value="A0_CUSTODY_ACCEPTED"
                    ),
                    PresentedOperatorActionPrecondition(
                        code="custody.lifecycle", expected_value="queued"
                    ),
                ),
                finished_copy="Pending custody cancel accepted. No broker order was sent.",
                signing_available=signing_available,
            )
        )
    return tuple(actions)


def _present_exact_cancels(
    *,
    account_id: str,
    snapshot_id: str,
    generated_at_ms: int,
    verdict: str,
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...],
    receipt: AccountReconciliationReceipt | None,
    signing_available: bool,
) -> tuple[PresentedOperatorAction, ...]:
    if (
        receipt is None
        or receipt.expires_at_ms <= generated_at_ms
        or verdict not in {"CLEAN", "SUSPENDED"}
        or receipt.account_id != account_id
        or receipt.connected_account_id != account_id
    ):
        return ()
    actions: list[PresentedOperatorAction] = []
    for order in receipt.account_truth.orders:
        if (
            order.fact_kind != "open_order"
            or order.account_id != account_id
            or order.order_ref is None
            or order.remaining <= 0
            or not order.cancel_action.enabled
        ):
            continue
        target = PresentedOperatorActionTarget(
            account_id=account_id,
            kind="EXACT_ORDER",
            target_order_id=order.order_id,
            target_order_ref=order.order_ref,
        )
        actions.append(
            _action(
                action_id="cancel_exact",
                target=target,
                snapshot_id=snapshot_id,
                generated_at_ms=generated_at_ms,
                evidence_refs=evidence_refs,
                effect_class="RISK_REDUCING_BROKER",
                confirmation=OperatorConfirmationCopy(
                    title="Cancel exact broker order",
                    body="Cancel only the current broker order identified in this safety snapshot.",
                    consequence="If the order filled, became terminal, or its identity changed, the Clerk will reject this action rather than cancel a replacement.",
                    confirm_label="Cancel exact order",
                ),
                availability="AVAILABLE" if signing_available else "UNAVAILABLE",
                preconditions=_preconditions(
                    snapshot_id=snapshot_id,
                    receipt=receipt,
                    values=(("broker.order.identity", f"{order.order_id}:{order.order_ref}"),),
                ),
                finished_copy="Exact cancel accepted by the Clerk. Refresh account evidence for the observed broker result.",
                signing_available=signing_available,
            )
        )
    return tuple(actions)


def _present_exact_recovery_flattens(
    *,
    account_id: str,
    snapshot_id: str,
    generated_at_ms: int,
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...],
    receipt: AccountReconciliationReceipt | None,
    candidates: Iterable[AccountRecoveryFlattenCandidate],
    signing_available: bool,
) -> tuple[PresentedOperatorAction, ...]:
    if receipt is None or receipt.expires_at_ms <= generated_at_ms:
        return ()
    actions: list[PresentedOperatorAction] = []
    for candidate in candidates:
        try:
            spec = IbkrOrderSpec.model_validate(candidate.intent.order_spec)
        except ValueError:
            continue
        if spec.con_id is None or spec.quantity <= 0:
            continue
        expected_quantity = spec.quantity if spec.action == "SELL" else -spec.quantity
        target = PresentedOperatorActionTarget(
            account_id=account_id,
            strategy_instance_id=candidate.intent.strategy_instance_id,
            run_id=candidate.intent.run_id,
            kind="RECOVERY_NAMESPACE",
            recovery_intent_id=candidate.intent.intent_id,
            recovery_order_ref=candidate.intent.order_ref,
            target_con_id=spec.con_id,
            expected_signed_quantity=expected_quantity,
        )
        actions.append(
            _action(
                action_id="flatten",
                target=target,
                snapshot_id=snapshot_id,
                generated_at_ms=generated_at_ms,
                evidence_refs=evidence_refs,
                effect_class="RISK_REDUCING_BROKER",
                confirmation=OperatorConfirmationCopy(
                    title="Submit exact recovery flatten",
                    body="Cancel the retired namespace's open orders, then submit the server-derived recovery order for this exact current position.",
                    consequence="The Clerk revalidates the target immediately before broker submission. Fresh reconciliation is required before reporting it flat.",
                    confirm_label="Submit recovery flatten",
                    required_token="FLATTEN",
                ),
                availability="AVAILABLE" if signing_available else "UNAVAILABLE",
                preconditions=_preconditions(
                    snapshot_id=snapshot_id,
                    receipt=receipt,
                    values=(
                        ("recovery.intent_id", candidate.intent.intent_id),
                        ("broker.position", f"{spec.con_id}:{expected_quantity}"),
                    ),
                ),
                finished_copy=(
                    "Recovery flatten accepted by the Clerk. Fresh reconciliation is required before the position is reported flat."
                ),
                signing_available=signing_available,
            )
        )
    return tuple(actions)


def _preconditions(
    *,
    snapshot_id: str,
    receipt: AccountReconciliationReceipt | None,
    values: tuple[tuple[str, str], ...],
) -> tuple[PresentedOperatorActionPrecondition, ...]:
    receipt_conditions = () if receipt is None else (
        PresentedOperatorActionPrecondition(
            code="reconciliation.receipt_id", expected_value=receipt.receipt_id
        ),
    )
    return (
        PresentedOperatorActionPrecondition(
            code="account_safety.snapshot_id", expected_value=snapshot_id
        ),
        *receipt_conditions,
        *(PresentedOperatorActionPrecondition(code=code, expected_value=value) for code, value in values),
    )


def _action(
    *,
    action_id: str,
    target: PresentedOperatorActionTarget,
    snapshot_id: str,
    generated_at_ms: int,
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...],
    effect_class: str,
    confirmation: OperatorConfirmationCopy,
    availability: str,
    preconditions: tuple[PresentedOperatorActionPrecondition, ...],
    finished_copy: str,
    signing_available: bool,
) -> PresentedOperatorAction:
    idempotency_key = hashlib.sha256(
        json.dumps(
            {
                "action_id": action_id,
                "snapshot_id": snapshot_id,
                "target": target.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    expires_at_ms = generated_at_ms + _PRESENTATION_TTL_MS
    return PresentedOperatorAction(
        action_id=action_id,
        target=target,
        snapshot_id=snapshot_id,
        snapshot_version=snapshot_id,
        evidence_refs=evidence_refs,
        effect_class=effect_class,
        idempotency_key=idempotency_key,
        issued_at_ms=generated_at_ms,
        expires_at_ms=expires_at_ms,
        presentation_token=(
            issue_presented_operator_action_token(
                action_id=action_id,
                target=target,
                snapshot_id=snapshot_id,
                snapshot_version=snapshot_id,
                idempotency_key=idempotency_key,
                issued_at_ms=generated_at_ms,
                expires_at_ms=expires_at_ms,
            )
            if signing_available
            else None
        ),
        preconditions=preconditions,
        confirmation=confirmation,
        availability=availability,
        disposition="fix_here" if availability == "AVAILABLE" else "wait",
        finished_copy=finished_copy,
    )
