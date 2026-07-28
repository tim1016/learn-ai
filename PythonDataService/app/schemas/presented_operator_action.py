"""Dependency-light signed action envelope primitives.

These request primitives are intentionally separate from the account-safety
snapshot composition.  Lifecycle request schemas import them during bootstrap,
while the snapshot composition itself depends on account evidence that already
uses live-run wire models.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings

PresentedOperatorActionId = Literal[
    "reconcile_now",
    "cancel_exact",
    "cancel_pending",
    "flatten",
    "pause",
    "stop",
    "end_day",
    "resume",
    "start",
    "deploy",
]
PresentedOperatorEffectClass = Literal[
    "EVIDENCE_REFRESH",
    "RISK_REDUCING_BROKER",
    "RISK_REDUCING_LIFECYCLE",
    "RISK_INCREASING_LIFECYCLE",
]
PresentedOperatorActionTargetKind = Literal[
    "ACCOUNT",
    "EXACT_ORDER",
    "A0_INTENT",
    "RECOVERY_NAMESPACE",
    "ACCOUNT_EMERGENCY",
]
PresentedOperatorActionAvailability = Literal["AVAILABLE", "UNAVAILABLE"]
PresentedOperatorActionDisposition = Literal["fix_here", "wait"]
_UNAUTHENTICATED_LOCAL_PRESENTATION_KEY = b"learn-ai/local-presented-action-envelope/v1"


class PresentedOperatorActionEffectReceipt(BaseModel):
    """Typed evidence returned by a broker-affecting presented action.

    This is intentionally a compact cross-process receipt reference rather
    than a second mutable broker model. The full Clerk journal receipt remains
    canonical; these immutable identifiers let every UI host link the action
    attempt, Clerk custody row, and any post-action reconciliation receipt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[
        "A0_CANCELLED",
        "EXACT_CANCEL_CONFIRMED",
        "RECOVERY_FLATTEN_SUBMITTED",
        "ACCOUNT_FLATTEN_OBSERVED",
        "FLATTEN_INTENTION_RECORDED",
    ]
    account_id: str = Field(min_length=1, max_length=64)
    intent_id: str | None = Field(default=None, min_length=1, max_length=128)
    order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    clerk_journal_seq: int | None = Field(default=None, ge=1)
    operation_id: str | None = Field(default=None, min_length=1, max_length=160)
    recorded_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> PresentedOperatorActionEffectReceipt:
        if self.kind == "FLATTEN_INTENTION_RECORDED":
            if (
                self.intent_id is not None
                or self.order_ref is not None
                or self.clerk_journal_seq is not None
                or self.operation_id is not None
            ):
                raise ValueError("flatten intention receipt must not claim a Clerk broker write")
            return self
        if self.kind == "ACCOUNT_FLATTEN_OBSERVED":
            if self.operation_id is None:
                raise ValueError("account flatten receipt requires operation identity")
            if self.intent_id is not None or self.order_ref is not None or self.clerk_journal_seq is not None:
                raise ValueError("account flatten receipt must not claim one intent identity")
            return self
        if self.intent_id is None or self.order_ref is None or self.clerk_journal_seq is None:
            raise ValueError("Clerk action receipt requires durable intent identity")
        if self.operation_id is not None:
            raise ValueError("Clerk action receipt must not claim account-wide operation identity")
        return self


class PresentedOperatorActionTarget(BaseModel):
    """Closed account/intent/order identity bound into one action.

    The browser may echo these fields only as part of the signed envelope. A
    target kind makes the broker-affecting variants unambiguous: an exact
    cancel cannot drift to a reused broker id, an A0 cancel cannot pretend a
    broker write occurred, and a recovery flatten retains the exact server
    authored namespace/position identity that must be re-proven at dispatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=160)
    kind: PresentedOperatorActionTargetKind = "ACCOUNT"
    target_order_id: int | None = Field(default=None, ge=0)
    target_order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    target_intent_id: str | None = Field(default=None, min_length=1, max_length=128)
    target_intent_order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    recovery_intent_id: str | None = Field(default=None, min_length=1, max_length=128)
    recovery_order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    target_con_id: int | None = Field(default=None, ge=1)
    expected_signed_quantity: float | None = None

    @model_validator(mode="after")
    def validate_exact_identity(self) -> PresentedOperatorActionTarget:
        """Keep each target form closed before it reaches a presentation token."""

        identity_fields = (
            self.target_order_id,
            self.target_order_ref,
            self.target_intent_id,
            self.target_intent_order_ref,
            self.recovery_intent_id,
            self.recovery_order_ref,
            self.target_con_id,
            self.expected_signed_quantity,
        )
        if self.kind == "ACCOUNT":
            if any(value is not None for value in identity_fields):
                raise ValueError("ACCOUNT target must not carry broker or custody identity")
            return self
        if self.kind == "ACCOUNT_EMERGENCY":
            if any(value is not None for value in identity_fields):
                raise ValueError("ACCOUNT_EMERGENCY target must not carry a narrower identity")
            return self
        if self.kind == "EXACT_ORDER":
            if self.target_order_id is None or self.target_order_ref is None:
                raise ValueError("EXACT_ORDER target requires order id and order reference")
            if any(value is not None for value in identity_fields[2:]):
                raise ValueError("EXACT_ORDER target only accepts one broker order identity")
            return self
        if self.kind == "A0_INTENT":
            if self.target_intent_id is None or self.target_intent_order_ref is None:
                raise ValueError("A0_INTENT target requires intent id and order reference")
            if self.strategy_instance_id is None or self.run_id is None:
                raise ValueError("A0_INTENT target requires one strategy instance and run")
            if any(value is not None for value in (self.target_order_id, self.target_order_ref, self.recovery_intent_id, self.recovery_order_ref, self.target_con_id, self.expected_signed_quantity)):
                raise ValueError("A0_INTENT target only accepts one local custody identity")
            return self
        if self.kind != "RECOVERY_NAMESPACE":
            raise ValueError(f"unhandled presented action target kind: {self.kind}")
        if (
            self.recovery_intent_id is None
            or self.recovery_order_ref is None
            or self.target_con_id is None
            or self.expected_signed_quantity is None
        ):
            raise ValueError("RECOVERY_NAMESPACE target requires exact recovery and position identity")
        if self.strategy_instance_id is None or self.run_id is None:
            raise ValueError("RECOVERY_NAMESPACE target requires one strategy instance and run")
        if not math.isfinite(self.expected_signed_quantity) or self.expected_signed_quantity == 0:
            raise ValueError("RECOVERY_NAMESPACE expected_signed_quantity must be finite and non-zero")
        if any(value is not None for value in (self.target_order_id, self.target_order_ref, self.target_intent_id, self.target_intent_order_ref)):
            raise ValueError("RECOVERY_NAMESPACE target only accepts recovery identity")
        return self


class PresentedOperatorActionPrecondition(BaseModel):
    """One server-owned fact that must still hold when an action executes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    expected_value: str = Field(min_length=1, max_length=160)


class PresentedOperatorActionInvocation(BaseModel):
    """The browser may return only this closed, signed-by-snapshot envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: PresentedOperatorActionId
    target: PresentedOperatorActionTarget
    snapshot_id: str = Field(min_length=16, max_length=64)
    snapshot_version: str = Field(min_length=16, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=160)
    issued_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    presentation_token: str = Field(min_length=64, max_length=64)
    confirmation_token: str | None = Field(default=None, min_length=1, max_length=64)


def issue_presented_operator_action_token(
    *,
    action_id: PresentedOperatorActionId,
    target: PresentedOperatorActionTarget,
    snapshot_id: str,
    snapshot_version: str,
    idempotency_key: str,
    issued_at_ms: int,
    expires_at_ms: int,
) -> str:
    """Sign one presentation lifetime without persisting during a read."""

    payload = json.dumps(
        {
            "action_id": action_id,
            "target": target.model_dump(mode="json"),
            "snapshot_id": snapshot_id,
            "snapshot_version": snapshot_version,
            "idempotency_key": idempotency_key,
            "issued_at_ms": issued_at_ms,
            "expires_at_ms": expires_at_ms,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(_presented_action_signing_key(), payload, hashlib.sha256).hexdigest()


def has_valid_presented_operator_action_token(
    invocation: PresentedOperatorActionInvocation,
) -> bool:
    """Verify an opaque presentation token before reading/replaying an attempt."""

    try:
        expected = issue_presented_operator_action_token(
            action_id=invocation.action_id,
            target=invocation.target,
            snapshot_id=invocation.snapshot_id,
            snapshot_version=invocation.snapshot_version,
            idempotency_key=invocation.idempotency_key,
            issued_at_ms=invocation.issued_at_ms,
            expires_at_ms=invocation.expires_at_ms,
        )
    except RuntimeError:
        return False
    return hmac.compare_digest(invocation.presentation_token, expected)


def presented_operator_action_signing_available() -> bool:
    """Whether this process can issue server-verifiable action presentations."""

    return bool(settings.DATA_PLANE_CONTROL_SECRET.strip()) or settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL


def _presented_action_signing_key() -> bytes:
    configured_secret = settings.DATA_PLANE_CONTROL_SECRET.strip().encode()
    if configured_secret:
        return hmac.new(
            configured_secret,
            b"learn-ai/presented-action-envelope/v1",
            hashlib.sha256,
        ).digest()
    if settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL:
        return _UNAUTHENTICATED_LOCAL_PRESENTATION_KEY
    raise RuntimeError("Presented action signing requires DATA_PLANE_CONTROL_SECRET.")
