"""Server-derived classification for account broker-write effects.

``risk_reducing`` is not a client capability.  A caller may name a desired
target, but this module derives the only effect class from a fresh, durable
Account Truth projection and the Clerk's current epoch.  The Clerk evaluates
the same request once before durable admission and again immediately before a
broker write, so a presentation-time result cannot authorize target drift.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.ibkr.account_truth_freshness import critical_source_freshness_blocks
from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_effect_models import (
    AccountEffectClass,
    AccountEffectEvidence,
    AccountEffectPurpose,
    AccountEffectRequest,
)
from app.engine.live.account_epoch import AccountEpoch, AccountEpochState
from app.schemas.account_truth import AccountTruthOrderRow, AccountTruthPositionRow, AccountTruthResponse

if TYPE_CHECKING:
    from app.engine.live.account_owner import AccountOwnerSubmitIntent
    from app.schemas.account_reconciliation import AccountReconciliationReceipt

_QUANTITY_ABS_TOLERANCE = 1e-9


class AccountEffectProjection(BaseModel):
    """One durable Account Truth snapshot tied to the current Clerk epoch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    receipt_id: str = Field(min_length=1, max_length=160)
    observed_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    epoch: AccountEpoch
    epoch_observed_at_ms: int = Field(ge=0)
    truth: AccountTruthResponse

    @model_validator(mode="after")
    def validate_projection_identity(self) -> AccountEffectProjection:
        truth_account_id = self.truth.account_id or self.truth.health.account_id
        if truth_account_id != self.account_id:
            raise ValueError("ACCOUNT_EFFECT_PROJECTION_ACCOUNT_MISMATCH")
        if self.expires_at_ms < self.observed_at_ms:
            raise ValueError("ACCOUNT_EFFECT_PROJECTION_EXPIRY_BEFORE_OBSERVATION")
        return self


class AccountEffectClassifier:
    """Read the canonical reconciliation receipt without refreshing IBKR."""

    def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
        self._account_id = account_id
        self._artifacts_root = artifacts_root

    def classify(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        epoch_state: AccountEpochState | None,
        now_ms: int,
    ) -> AccountEffectEvidence:
        """Classify using cached durable evidence only; this never calls IBKR."""

        # Import at the read boundary rather than module scope: the
        # reconciliation service legitimately imports Clerk read helpers.
        # The classifier must not create a service-import cycle at boot.
        from app.services.account_reconciliation import AccountReconciliationService

        receipt = AccountReconciliationService(
            artifacts_root=self._artifacts_root
        ).read_latest_receipt(self._account_id)
        projection = _projection_from_receipt(receipt, epoch_state)
        return classify_account_effect(intent, projection=projection, now_ms=now_ms)


def _projection_from_receipt(
    receipt: AccountReconciliationReceipt | None,
    epoch_state: AccountEpochState | None,
) -> AccountEffectProjection | None:
    if receipt is None or epoch_state is None:
        return None
    if receipt.account_id != epoch_state.account_id:
        return None
    return AccountEffectProjection(
        account_id=receipt.account_id,
        receipt_id=receipt.receipt_id,
        observed_at_ms=receipt.account_truth_generated_at_ms,
        expires_at_ms=receipt.expires_at_ms,
        epoch=epoch_state.current_epoch,
        epoch_observed_at_ms=epoch_state.updated_at_ms,
        truth=receipt.account_truth,
    )


def classify_account_effect(
    intent: AccountOwnerSubmitIntent,
    *,
    projection: AccountEffectProjection | None,
    now_ms: int,
) -> AccountEffectEvidence:
    """Classify one requested broker action from immutable current evidence."""

    request = intent.effect_request
    if request is None:
        if intent.intent_kind in {"RECOVERY_FLATTEN", "EMERGENCY_FLATTEN"}:
            return _evidence(
                intent,
                effect_class=AccountEffectClass.ACCOUNT_EMERGENCY,
                reason_code="ACCOUNT_EMERGENCY_OPERATION_REQUIRED",
                projection=projection,
                now_ms=now_ms,
            )
        return _evidence(
            intent,
            effect_class=AccountEffectClass.ENTRY,
            reason_code="ENTRY_REQUEST",
            projection=projection,
            now_ms=now_ms,
        )

    if projection is None:
        return _ambiguous(intent, "EFFECT_PROJECTION_UNAVAILABLE", now_ms=now_ms)
    if projection.account_id != intent.account_id:
        return _ambiguous(intent, "EFFECT_PROJECTION_ACCOUNT_MISMATCH", now_ms=now_ms)
    if now_ms > projection.expires_at_ms:
        return _ambiguous(intent, "EFFECT_PROJECTION_EXPIRED", projection=projection, now_ms=now_ms)
    # Timestamps are millisecond-resolution. Equality does not establish that
    # the reconciliation receipt observed an epoch invalidation that happened
    # in that same millisecond, so only evidence strictly after the epoch is
    # eligible to authorize a reducing broker effect.
    if projection.observed_at_ms <= projection.epoch_observed_at_ms:
        return _ambiguous(intent, "EFFECT_PROJECTION_EPOCH_UNPROVEN", projection=projection, now_ms=now_ms)
    if critical_source_freshness_blocks(projection.truth.source_freshness, checked_at_ms=now_ms):
        return _ambiguous(intent, "EFFECT_CRITICAL_SOURCE_STALE", projection=projection, now_ms=now_ms)

    if request.purpose is AccountEffectPurpose.EXACT_CANCEL:
        return _classify_exact_cancel(intent, request, projection=projection, now_ms=now_ms)
    return _classify_exact_close(intent, request, projection=projection, now_ms=now_ms)


def _classify_exact_cancel(
    intent: AccountOwnerSubmitIntent,
    request: AccountEffectRequest,
    *,
    projection: AccountEffectProjection,
    now_ms: int,
) -> AccountEffectEvidence:
    matching = [
        order
        for order in projection.truth.orders
        if order.fact_kind == "open_order"
        and order.order_id == request.target_order_id
        and order.order_ref == request.target_order_ref
        and order.account_id == intent.account_id
        and order.remaining > 0
    ]
    if len(matching) != 1:
        return _ambiguous(intent, "EXACT_CANCEL_TARGET_NOT_CURRENT", projection=projection, now_ms=now_ms)
    order = matching[0]
    return _evidence(
        intent,
        effect_class=AccountEffectClass.EXACT_CANCEL,
        reason_code="EXACT_CURRENT_ORDER_CANCEL",
        projection=projection,
        target_order=order,
        now_ms=now_ms,
    )


def _classify_exact_close(
    intent: AccountOwnerSubmitIntent,
    request: AccountEffectRequest,
    *,
    projection: AccountEffectProjection,
    now_ms: int,
) -> AccountEffectEvidence:
    matching = [
        position
        for position in projection.truth.positions
        if position.account_id == intent.account_id and position.con_id == request.target_con_id
    ]
    if len(matching) != 1:
        return _ambiguous(intent, "EXACT_CLOSE_TARGET_NOT_CURRENT", projection=projection, now_ms=now_ms)
    position = matching[0]
    if not math.isclose(
        position.quantity,
        request.expected_signed_quantity or 0.0,
        abs_tol=_QUANTITY_ABS_TOLERANCE,
        rel_tol=0.0,
    ):
        return _ambiguous(intent, "EXACT_CLOSE_POSITION_DRIFTED", projection=projection, now_ms=now_ms)
    try:
        spec = IbkrOrderSpec.model_validate(intent.order_spec)
    except ValueError:
        return _ambiguous(intent, "EXACT_CLOSE_ORDER_SPEC_INVALID", projection=projection, now_ms=now_ms)
    if spec.order_ref != intent.order_ref:
        return _ambiguous(
            intent,
            "EXACT_CLOSE_ORDER_REF_MISMATCH",
            projection=projection,
            now_ms=now_ms,
        )
    if spec.con_id != request.target_con_id:
        return _ambiguous(
            intent,
            "EXACT_CLOSE_EXECUTION_IDENTITY_MISMATCH",
            projection=projection,
            now_ms=now_ms,
        )
    expected_action = "SELL" if position.quantity > 0 else "BUY"
    if (
        position.sec_type != "STK"
        or spec.sec_type != position.sec_type
        or spec.symbol != position.symbol
        or spec.action != expected_action
        or spec.quantity > abs(position.quantity)
        or math.isclose(spec.quantity, 0.0, abs_tol=_QUANTITY_ABS_TOLERANCE, rel_tol=0.0)
    ):
        return _ambiguous(intent, "EXACT_CLOSE_WORST_CASE_NOT_REDUCING", projection=projection, now_ms=now_ms)
    return _evidence(
        intent,
        effect_class=AccountEffectClass.EXACT_CLOSE,
        reason_code="EXACT_CURRENT_POSITION_CLOSE",
        projection=projection,
        target_position=position,
        requested_quantity=spec.quantity,
        now_ms=now_ms,
    )


def _ambiguous(
    intent: AccountOwnerSubmitIntent,
    reason_code: str,
    *,
    projection: AccountEffectProjection | None = None,
    now_ms: int,
) -> AccountEffectEvidence:
    return _evidence(
        intent,
        effect_class=AccountEffectClass.AMBIGUOUS,
        reason_code=reason_code,
        projection=projection,
        now_ms=now_ms,
    )


def _evidence(
    intent: AccountOwnerSubmitIntent,
    *,
    effect_class: AccountEffectClass,
    reason_code: str,
    projection: AccountEffectProjection | None,
    now_ms: int,
    target_order: AccountTruthOrderRow | None = None,
    target_position: AccountTruthPositionRow | None = None,
    requested_quantity: float | None = None,
) -> AccountEffectEvidence:
    target_order_id = target_order.order_id if target_order is not None else None
    target_order_ref = target_order.order_ref if target_order is not None else None
    target_con_id = target_position.con_id if target_position is not None else None
    target_signed_quantity = target_position.quantity if target_position is not None else None
    classification_id = _classification_id(
        intent=intent,
        effect_class=effect_class,
        reason_code=reason_code,
        projection=projection,
        target_order_id=target_order_id,
        target_order_ref=target_order_ref,
        target_con_id=target_con_id,
        target_signed_quantity=target_signed_quantity,
        requested_quantity=requested_quantity,
    )
    return AccountEffectEvidence(
        classification_id=classification_id,
        effect_class=effect_class,
        reason_code=reason_code,
        account_id=intent.account_id,
        intent_id=intent.intent_id,
        order_ref=intent.order_ref,
        projection_receipt_id=(projection.receipt_id if projection is not None else None),
        projection_observed_at_ms=(projection.observed_at_ms if projection is not None else None),
        epoch=(projection.epoch if projection is not None else None),
        target_order_id=target_order_id,
        target_order_ref=target_order_ref,
        target_con_id=target_con_id,
        target_signed_quantity=target_signed_quantity,
        requested_quantity=requested_quantity,
        evaluated_at_ms=now_ms,
    )


def _classification_id(
    *,
    intent: AccountOwnerSubmitIntent,
    effect_class: AccountEffectClass,
    reason_code: str,
    projection: AccountEffectProjection | None,
    target_order_id: int | None,
    target_order_ref: str | None,
    target_con_id: int | None,
    target_signed_quantity: float | None,
    requested_quantity: float | None,
) -> str:
    payload = {
        "account_id": intent.account_id,
        "intent_id": intent.intent_id,
        "order_ref": intent.order_ref,
        "intent_kind": intent.intent_kind,
        "effect_request": intent.effect_request.model_dump(mode="json") if intent.effect_request else None,
        "effect_class": effect_class.value,
        "reason_code": reason_code,
        "projection_receipt_id": projection.receipt_id if projection is not None else None,
        "projection_observed_at_ms": projection.observed_at_ms if projection is not None else None,
        "epoch": projection.epoch.model_dump(mode="json") if projection is not None else None,
        "epoch_observed_at_ms": projection.epoch_observed_at_ms if projection is not None else None,
        "target_order_id": target_order_id,
        "target_order_ref": target_order_ref,
        "target_con_id": target_con_id,
        "target_signed_quantity": target_signed_quantity,
        "requested_quantity": requested_quantity,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "AccountEffectClass",
    "AccountEffectClassifier",
    "AccountEffectEvidence",
    "AccountEffectProjection",
    "AccountEffectPurpose",
    "AccountEffectRequest",
    "classify_account_effect",
]
