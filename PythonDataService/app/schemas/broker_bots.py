"""Request/response schemas for the broker-parameterized bot runner routes.

``/api/brokers/{broker}/bots/...`` (Alpaca Bot Control v2, S2 — #1260).
Views are projections of the durable lifecycle artifacts; the router never
derives state that is not artifact- or registry-backed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.live_runs import BotDutyOutcomeView


class DeployBotRequest(BaseModel):
    """Deploy (and start) a bot bound to ``{broker}``."""

    model_config = ConfigDict(extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=12)
    use_rth: bool = True
    mode: Literal["log_only", "trade"] = "log_only"
    quantity: int = Field(default=1, ge=1, le=100)

    @field_validator("strategy_instance_id")
    @classmethod
    def _validate_strategy_instance_id(cls, value: str) -> str:
        # Canonical path-segment validation — same rule every artifact path
        # builder enforces, applied at the API boundary so a bad id fails as
        # 422 instead of a 500 from a path builder.
        from app.engine.live.identity import validate_strategy_instance_id

        return validate_strategy_instance_id(value)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalnum():
            raise ValueError("symbol must be alphanumeric")
        return normalized


class StopBotRequest(BaseModel):
    """Button-Rule exit: stop a running bot (durable desired-state first)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=256)


class BotStatusView(BaseModel):
    """One bot's roster row: broker-tagged binding + artifact-derived state."""

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    broker: str
    symbol: str
    mode: Literal["log_only", "trade"]
    quantity: int
    running: bool
    phase: Literal["OFF_DUTY", "ON_DUTY", "RETIRED"]
    desired_state: Literal["RUNNING", "PAUSED", "STOPPED"]
    active_run_id: str | None
    duty_outcome: BotDutyOutcomeView | None
    binding_created_at_ms: int
    last_transition_at_ms: int | None
