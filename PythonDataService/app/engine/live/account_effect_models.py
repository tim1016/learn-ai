"""Small request contracts for server-derived broker-effect classification."""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.engine.live.account_epoch import AccountEpoch

_QUANTITY_ABS_TOLERANCE = 1e-9


class AccountEffectPurpose(StrEnum):
    """The limited target shapes a caller may ask the server to verify."""

    EXACT_CANCEL = "EXACT_CANCEL"
    EXACT_CLOSE = "EXACT_CLOSE"


class AccountEffectClass(StrEnum):
    """Closed effect vocabulary used at the account-safety boundary."""

    ENTRY = "ENTRY"
    EXACT_CANCEL = "EXACT_CANCEL"
    EXACT_CLOSE = "EXACT_CLOSE"
    AMBIGUOUS = "AMBIGUOUS"
    ACCOUNT_EMERGENCY = "ACCOUNT_EMERGENCY"


class AccountEffectRequest(BaseModel):
    """Untrusted requested target; never itself proves a safe effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    purpose: AccountEffectPurpose
    target_order_id: int | None = Field(default=None, ge=0)
    target_order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    target_con_id: int | None = Field(default=None, ge=1)
    expected_signed_quantity: float | None = None

    @model_validator(mode="after")
    def validate_exact_target(self) -> AccountEffectRequest:
        if self.purpose is AccountEffectPurpose.EXACT_CANCEL:
            if self.target_order_id is None or self.target_order_ref is None:
                raise ValueError("EXACT_CANCEL requires target_order_id and target_order_ref")
            if self.target_con_id is not None or self.expected_signed_quantity is not None:
                raise ValueError("EXACT_CANCEL only accepts the exact order identity")
        elif self.purpose is AccountEffectPurpose.EXACT_CLOSE:
            if self.target_con_id is None or self.expected_signed_quantity is None:
                raise ValueError("EXACT_CLOSE requires target_con_id and expected_signed_quantity")
            if (
                not math.isfinite(self.expected_signed_quantity)
                or math.isclose(
                    self.expected_signed_quantity,
                    0.0,
                    abs_tol=_QUANTITY_ABS_TOLERANCE,
                    rel_tol=0.0,
                )
            ):
                raise ValueError("EXACT_CLOSE expected_signed_quantity must be finite and non-zero")
            if self.target_order_id is not None or self.target_order_ref is not None:
                raise ValueError("EXACT_CLOSE only accepts the exact position identity")
        return self


class AccountEffectEvidence(BaseModel):
    """Backend-authored effect decision retained with a Clerk receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification_id: str = Field(min_length=64, max_length=64)
    effect_class: AccountEffectClass
    reason_code: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=64)
    intent_id: str = Field(min_length=1, max_length=128)
    order_ref: str = Field(min_length=1, max_length=512)
    projection_receipt_id: str | None = Field(default=None, min_length=1, max_length=160)
    projection_observed_at_ms: int | None = Field(default=None, ge=0)
    epoch: AccountEpoch | None = None
    target_order_id: int | None = Field(default=None, ge=0)
    target_order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    target_con_id: int | None = Field(default=None, ge=1)
    target_signed_quantity: float | None = None
    requested_quantity: float | None = None
    evaluated_at_ms: int = Field(ge=0)

    @property
    def permits_suspended_admission(self) -> bool:
        """Whether this effect has a broker-atomic suspension exception.

        IBKR stock orders do not provide a reduce-only guard. A client-side
        position snapshot cannot make an exact close atomic with a late fill,
        so only cancellation of an exactly identified open order is eligible
        until the broker execution contract grows such a primitive.
        """

        return self.effect_class is AccountEffectClass.EXACT_CANCEL


__all__ = [
    "AccountEffectClass",
    "AccountEffectEvidence",
    "AccountEffectPurpose",
    "AccountEffectRequest",
]
