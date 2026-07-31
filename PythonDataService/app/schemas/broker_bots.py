"""Request/response schemas for the broker-parameterized bot runner routes.

``/api/brokers/{broker}/bots/...`` (Alpaca Bot Control v2, S2 — #1260).
Views are projections of the durable lifecycle artifacts; the router never
derives state that is not artifact- or registry-backed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.action_plan import ActionPlan
from app.schemas.live_runs import BotDutyOutcomeView


def _validated_strategy_instance_id(value: str) -> str:
    from app.engine.live.identity import validate_strategy_instance_id

    return validate_strategy_instance_id(value)


def _normalized_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized.isalnum():
        raise ValueError("symbol must be alphanumeric")
    return normalized


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
        return _validated_strategy_instance_id(value)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)


class AlpacaPaperSizingSelection(BaseModel):
    """One closed sizing choice for the Alpaca paper canary workflow."""

    model_config = ConfigDict(extra="forbid")

    preset: Literal["safe_canary", "custom"] = "safe_canary"
    quantity: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _safe_canary_is_one_share(self) -> AlpacaPaperSizingSelection:
        if self.preset == "safe_canary" and self.quantity != 1:
            raise ValueError("safe_canary sizing is fixed at exactly one share")
        return self


class AlpacaPaperDeployRequest(BaseModel):
    """Closed account-scoped command for the production Alpaca deploy page."""

    model_config = ConfigDict(extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    strategy_key: Literal["deployment_validation"]
    symbol: str = Field(min_length=1, max_length=12)
    sizing: AlpacaPaperSizingSelection = Field(
        default_factory=AlpacaPaperSizingSelection
    )
    carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID"

    @field_validator("strategy_instance_id")
    @classmethod
    def _validate_strategy_instance_id(cls, value: str) -> str:
        return _validated_strategy_instance_id(value)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)


class AlpacaPaperDeployEligibility(BaseModel):
    """Backend-authored launch verdict rendered verbatim by Angular."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible: bool
    reason_code: str
    headline: str
    explanation: str
    next_action: str


class AlpacaPaperDeployStrategy(BaseModel):
    """One validated strategy in the phase-1 closed catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_key: Literal["deployment_validation"]
    label: str
    explanation: str


class AlpacaPaperSizingOption(BaseModel):
    """Backend-authored sizing choice and its bounded quantity contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preset: Literal["safe_canary", "custom"]
    label: str
    explanation: str
    min_quantity: int = Field(ge=1)
    max_quantity: int = Field(ge=1)
    default_quantity: int = Field(ge=1)


class AlpacaPaperDeployView(BaseModel):
    """All semantics needed to render the Alpaca paper deploy workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker: Literal["alpaca"]
    account_id: str
    account_mode: Literal["paper"]
    account_label: str
    eligibility: AlpacaPaperDeployEligibility
    strategies: tuple[AlpacaPaperDeployStrategy, ...]
    sizing_options: tuple[AlpacaPaperSizingOption, ...]
    action_plan_explanation: str
    carryover_available: bool
    carryover_label: str
    carryover_explanation: str
    allowed_actions: tuple[Literal["deploy"], ...]


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
    carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID"
    carryover_account_policy_enabled: bool = False
    carryover_checkpoint_exposure: dict[str, float] = Field(default_factory=dict)
    carryover_checkpoint_config_matches: bool = False
    running: bool
    phase: Literal["OFF_DUTY", "ON_DUTY", "RETIRED"]
    desired_state: Literal["RUNNING", "PAUSED", "STOPPED"]
    active_run_id: str | None
    duty_outcome: BotDutyOutcomeView | None
    binding_created_at_ms: int
    last_transition_at_ms: int | None


class AlpacaPaperDeployReceipt(BaseModel):
    """Backend-authored terminal receipt for one accepted deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["deployed"]
    message: str
    explanation: str
    next_action: str
    panel_path: str
    action_plan: ActionPlan
    bot: BotStatusView
