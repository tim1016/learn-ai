"""Durable legacy Account Clerk intent models.

Executable AccountOwner and AccountClerk broker-write lanes retired in #1583.
The immutable intent schema remains because historical Clerk journals and the
Alpaca compatibility reader must continue to validate already-written rows.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.engine.live.account_effect_models import AccountEffectRequest

MANUAL_ORDER_INTENT_KIND = "MANUAL_ORDER"
MANUAL_OPERATOR_STRATEGY_INSTANCE_ID = "manual-operator"
MANUAL_OPERATOR_RUN_ID = "manual-ticket"


class AccountOwnerSubmitIntent(BaseModel):
    """Immutable historical intent recorded in Account Clerk journals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    strategy_instance_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    bot_order_namespace: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    order_ref: str = Field(min_length=1)
    intent_kind: str = Field(min_length=1)
    order_spec: dict
    effect_request: AccountEffectRequest | None = None
    owner_generation: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order_ref(self) -> AccountOwnerSubmitIntent:
        expected = f"{self.bot_order_namespace}:{self.intent_id}"
        if self.order_ref != expected:
            raise ValueError(f"order_ref {self.order_ref!r} != {expected!r}")
        return self


__all__ = [
    "MANUAL_OPERATOR_RUN_ID",
    "MANUAL_OPERATOR_STRATEGY_INSTANCE_ID",
    "MANUAL_ORDER_INTENT_KIND",
    "AccountOwnerSubmitIntent",
]
