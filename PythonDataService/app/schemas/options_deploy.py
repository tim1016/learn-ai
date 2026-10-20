"""Account-scoped deployment of an approved Options setup (ADR 0079)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from app.options.contract import OptionContract
from app.options_backtest.results import EventKind
from app.schemas.bot_end import BotEndInput, refuse_explicit_null_end
from app.schemas.broker_bots import SUBMISSION_KEY_PATTERN
from app.schemas.deployment_budget import DeploymentBudgetInput, DeploymentBudgetPreview
from app.schemas.exit_terms import ExitTerms, ExitTermsInput
from app.schemas.golden_option_setups import GoldenOptionSetupView
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionsDeployRequest(BaseModel):
    """Trading rules come from the approved version; only deployment settings are authored here."""

    model_config = ConfigDict(extra="forbid")

    golden_setup_id: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["paper", "dry_run", "shadow", "live"]
    budget: DeploymentBudgetInput | None = None
    order_terms: SealedOptionOrderTerms
    exit_terms: ExitTermsInput
    end: BotEndInput | SkipJsonSchema[None] = None

    @field_validator("end", mode="before")
    @classmethod
    def _end_is_never_null(cls, value: object) -> object:
        return refuse_explicit_null_end(value)

    def fingerprint(self, *, account: str, world: str) -> str:
        payload = self.model_dump(mode="json", exclude={"submission_key"})
        if payload["budget"] is not None:
            payload["budget"].pop("review_token", None)
            payload["budget"].pop("live_confirmation", None)
        return semantic_payload_hash({"kind": "options", "account": account, "world": world, "configuration": payload})


class OptionsDeploySubmission(OptionsDeployRequest):
    submission_key: str = Field(pattern=SUBMISSION_KEY_PATTERN)


class OptionsDeploymentReview(BaseModel):
    golden: GoldenOptionSetupView
    budget: DeploymentBudgetPreview
    allowed: bool
    explanation: str
    rules_summary: list[str]
    first_eligible_session_open_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)


class OptionsResumeRequest(BaseModel):
    """An explicit recovery command; its key never authorizes a second active run."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(pattern=SUBMISSION_KEY_PATTERN)
    budget: DeploymentBudgetInput | None = None


class OptionsWatchedPremiumView(BaseModel):
    symbol: str
    premium: str | None = None
    close_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)


class OptionsBotEventView(BaseModel):
    kind: EventKind
    at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    leg_id: str
    entry_number: int = Field(ge=0)
    contract: OptionContract | None = None
    price: str | None = None
    reason: str | None = None
    detail: str | None = None


class OptionsBotLegView(BaseModel):
    leg_id: str
    side: Literal["buy", "sell"]
    phase: str
    entries_filled: int
    reentries_left: int
    held_contract: str | None = None
    held_contracts: int
    average_fill_price: str | None = None
    stop_level: str | None = None
    waiting_contract: str | None = None
    starting_price: str | None = None
    momentum_level: str | None = None
    watched_contracts: list[OptionsWatchedPremiumView] = Field(default_factory=list)
    realized_pnl_usd: str | None = None
    open_pnl_usd: str | None = None
    pnl_notice: str | None = None


class OptionsBotSessionView(BaseModel):
    strategy_instance_id: str
    golden_setup_id: str
    session_open_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    session_close_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    last_minute_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    next_entry_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    entry_hold_reason: str | None = None
    execution_hold_reason: str | None = None
    pending_decisions: int
    entries_enabled: bool
    resume_allowed: bool
    resume_explanation: str
    legs: list[OptionsBotLegView]
    events: list[OptionsBotEventView]
    order_terms: SealedOptionOrderTerms | None = None
    exit_terms: ExitTerms | None = None
