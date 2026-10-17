"""Account-scoped deployment of an approved Options setup (ADR 0079)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bot_end import BotEndInput
from app.schemas.broker_bots import SUBMISSION_KEY_PATTERN
from app.schemas.deployment_budget import DeploymentBudgetInput, DeploymentBudgetPreview
from app.schemas.exit_terms import ExitTermsInput
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
    end: BotEndInput | None = None

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
    events: list[dict[str, object]]
