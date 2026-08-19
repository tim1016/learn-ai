"""Typed bot and Clerk facts consumed by the canonical run-admission policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.market_liveness import MarketLivenessFact


class RunProcessAdmissionFact(BaseModel):
    """Registry-owned process evidence, including proven candidate absence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal[
        "ABSENT",
        "STARTING",
        "RUNNING",
        "STOPPING",
        "EXITED",
        "UNKNOWN",
    ]
    run_id: str | None = None
    process_identity: str | None = None
    registry_generation: str
    observed_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _identity_matches_state(self) -> RunProcessAdmissionFact:
        if self.state == "ABSENT" and (self.run_id is not None or self.process_identity is not None):
            raise ValueError("an absent process cannot carry run identity")
        if self.state in {"STARTING", "RUNNING", "STOPPING"} and (self.run_id is None or self.process_identity is None):
            raise ValueError("a live process fact requires run and process identity")
        return self


class MarketDataAdmissionFact(BaseModel):
    """Bot-authority view of the feed needed by the proposed run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["AVAILABLE", "STALE", "UNAVAILABLE", "UNKNOWN"]
    feed_id: str | None = None
    last_bar_ms: int | None = Field(default=None, ge=0)
    observed_at_ms: int = Field(ge=0)
    reason: str | None = None
    connected: bool | None = None
    stale: bool | None = None
    active_subscription_count: int | None = Field(default=None, ge=0)
    scheduled_phase: Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"] = "UNKNOWN"
    session_authority_source: Literal["ibkr_capability", "nyse_calendar"] | None = None
    extended_phase_proven: bool = False


class StartRuntimeAdmissionFact(BaseModel):
    """Runner-owned recovery and restart-intensity evidence for Start."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal[
        "READY",
        "BOOT_RECOVERY_INCOMPLETE",
        "RECOVERY_UNCERTAIN",
        "RESTART_INTENSITY_EXCEEDED",
    ]
    observed_at_ms: int = Field(ge=0)
    explanation: str
    next_step: str | None = None


class StartRunFacts(BaseModel):
    """Bot-owned immutable and runtime facts for a proposed first run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["START"] = "START"
    strategy_instance_id: str
    proposed_run_id: str
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: StartRuntimeAdmissionFact
    process: RunProcessAdmissionFact
    market_data: MarketDataAdmissionFact
    market_liveness: MarketLivenessFact


class ResumeCheckpointAdmissionFact(BaseModel):
    """Durable STOP checkpoint compared with fresh Clerk custody on Resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    stopped_run_id: str
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    exposure: dict[str, float]
    approved: bool
    evidence_ref: str


class ResumeRunFacts(BaseModel):
    """Bot-owned immutable and lifecycle facts for a proposed new run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["RESUME"] = "RESUME"
    strategy_instance_id: str
    proposed_run_id: str
    prior_run_id: str
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: StartRuntimeAdmissionFact
    process: RunProcessAdmissionFact
    market_data: MarketDataAdmissionFact
    market_liveness: MarketLivenessFact
    desired_state: Literal["RUNNING", "PAUSED", "STOPPED"]
    phase: Literal["OFF_DUTY", "ON_DUTY", "RETIRED"]
    carryover_policy: Literal["FORBID", "ALLOW"]
    carryover_account_policy_enabled: bool
    exposure_carryover_supported: bool
    checkpoint: ResumeCheckpointAdmissionFact | None


RunAdmissionFacts = StartRunFacts | ResumeRunFacts


class RunAdmissionFactAges(BaseModel):
    """Age of every authority fact at the exact admission evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime: int
    process: int
    market_data: int
    market_liveness: int
    clerk: int


class RunAdmissionDecision(BaseModel):
    """Backend-authored permission and explanation rendered by every client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["START", "RESUME"]
    allowed: bool
    reason_code: str
    explanation: str
    next_step: str | None
    strategy_instance_id: str
    proposed_run_id: str
    configuration_hash: str
    account_id: str
    evaluated_at_ms: int = Field(ge=0)
    fact_ages_ms: RunAdmissionFactAges
    evidence_refs: tuple[str, ...]
