"""Request/response schemas for the broker-parameterized bot runner routes.

``/api/brokers/{broker}/bots/...`` (Alpaca Bot Control v2, S2 — #1260).
Views are projections of the durable lifecycle artifacts; the router never
derives state that is not artifact- or registry-backed.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.schemas.action_plan import ActionPlan
from app.schemas.live_runs import BotDutyOutcomeView


def _validated_strategy_instance_id(value: str) -> str:
    from app.engine.live.identity import validate_strategy_instance_id

    return validate_strategy_instance_id(value)


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")


class BotProcessFact(BaseModel):
    """Process-registry observation for one strategy run.

    This fact reports only process presence. It never implies broker custody,
    exposure, order state, or permission to trade.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    run_id: str
    process_identity: str | None
    state: Literal["STARTING", "RUNNING", "STOPPING", "EXITED", "UNKNOWN"]
    registry_generation: str
    observed_at_ms: int = Field(ge=0)


class BotControlAuthorityFacts(BaseModel):
    """Independent process and Clerk facts for one bot control decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process: BotProcessFact
    clerk: ClerkCustodySnapshot


class AlpacaPaperStrategyKey(StrEnum):
    """Strategies supported by the Clerk-governed Alpaca paper runner."""

    DEPLOYMENT_VALIDATION = "deployment_validation"
    EMA_CROSSOVER_SIGNAL = "ema_crossover_signal"


def _normalized_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if _SYMBOL_RE.fullmatch(normalized) is None:
        raise ValueError(
            "symbol must start with a letter and contain only letters, numbers, periods, or hyphens"
        )
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
    strategy_key: AlpacaPaperStrategyKey
    symbol: str = Field(min_length=1, max_length=12)
    sizing: AlpacaPaperSizingSelection = Field(default_factory=AlpacaPaperSizingSelection)
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
    """Trader-facing option for one currently accepted deploy strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_key: AlpacaPaperStrategyKey
    label: str
    explanation: str
    validation_case_symbol: str


class AlpacaPaperDeployReadinessCheck(BaseModel):
    """One production-backed predicate in the current Deploy admission decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str
    label: str
    ready: bool
    scope: Literal["strategy", "account", "broker"]
    authority: str
    headline: str
    explanation: str
    evidence_summary: str
    evidence: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    recovery: str | None


class AlpacaPaperExecutionMode(BaseModel):
    """Broker-authored execution capability for the shared Deploy page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["paper", "live"]
    label: str
    availability: Literal["available", "planned"]
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
    evaluated_at_ms: int = Field(ge=0)
    eligibility: AlpacaPaperDeployEligibility
    readiness_checks: tuple[AlpacaPaperDeployReadinessCheck, ...]
    execution_modes: tuple[AlpacaPaperExecutionMode, ...]
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
    strategy_key: str = "deployment_validation"
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
    outcome: Literal["success"] = "success"
    receipt_id: str
    recorded_at_ms: int = Field(ge=0)
    message: str
    explanation: str
    next_action: str
    panel_path: str
    account_id: str
    execution_mode: Literal["paper"] = "paper"
    sizing: AlpacaPaperSizingSelection
    carryover_policy: Literal["FORBID", "ALLOW"]
    action_plan: ActionPlan
    bot: BotStatusView
