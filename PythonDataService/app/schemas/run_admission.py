"""Typed bot and Clerk facts consumed by the canonical run-admission policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.marketdata.feed import BarSessionPhase
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
    scheduled_phase: BarSessionPhase = "UNKNOWN"
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


class StrategyValidationAdmissionFact(BaseModel):
    """Fresh verification of the exact strategy proof proposed for one run.

    The validation manifest remains the source of the human decision.  This
    fact is the admission-time receipt that its active event and referenced
    artifacts were re-read and re-hashed immediately before Start or Resume.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["VERIFIED", "UNVERIFIED", "UNREADABLE"]
    strategy_key: str
    evidence_status: Literal["accepted", "evidence_only", "blocked", "unknown"]
    event_id: str | None = None
    evidence_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verified_at_ms: int = Field(ge=0)
    evidence_refs: tuple[str, ...] = ()
    explanation: str
    next_step: str | None = None


#: The four fields that jointly constitute a PROVEN build's proof. Kept as a
#: single named tuple so the ``model_validator`` below and its tests share
#: one source of truth for "what does PROVEN require" rather than each
#: hand-listing the same four names.
PROGRAM_BUILD_PROOF_FIELDS: tuple[str, ...] = (
    "program_version",
    "golden_trace_root",
    "running_artifact_digest",
    "qualification_receipt_hash",
)


class ProgramBuildAdmissionFact(BaseModel):
    """Admission-time proof that loaded program bytes match qualification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["PROVEN", "UNPROVEN", "NOT_APPLICABLE"]
    program_key: str
    program_version: str | None = None
    golden_trace_root: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    running_artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    qualification_receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verified_at_ms: int = Field(ge=0)
    evidence_refs: tuple[str, ...] = ()
    explanation: str
    next_step: str | None = None
    # How this verdict was obtained. Admission (Start/Resume) always proves the
    # running bytes live, which is the default. A panel read replays the durable
    # per-run record instead, and says so rather than leaving a stale
    # ``verified_at_ms`` as the only clue that no re-proof happened.
    verification: Literal["live_reproof", "frozen_run_evidence"] = "live_reproof"
    # Issue #1735. Whether the strategy *wiring* half of the build still
    # matches its receipt. ``DRIFTED`` alongside ``state="PROVEN"`` is the
    # warning-only posture: the coverage is new, so until
    # ``SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED`` is turned on a wiring drift is
    # reported rather than blocking. A drift in the already-covered artifacts
    # is not representable here -- it fails closed as UNPROVEN, unchanged.
    wiring: Literal["MATCHED", "DRIFTED", "NOT_CHECKED"] = "NOT_CHECKED"

    @model_validator(mode="after")
    def _proven_carries_its_full_proof(self) -> ProgramBuildAdmissionFact:
        """``state == "PROVEN"`` must carry the full proof, not a partial one.

        UNPROVEN and NOT_APPLICABLE are deliberately left unconstrained here:
        both producers (``prove_running_program_build`` and
        ``program_build_view_from_run_evidence``) only ever leave the proof
        fields absent for those states today, but nothing about "not proven"
        requires that — a future diagnostic case that attaches a partial,
        non-authoritative proof to an UNPROVEN fact would be legitimate, and
        must not be refused by this validator.
        """
        if self.state != "PROVEN":
            return self
        missing = [name for name in PROGRAM_BUILD_PROOF_FIELDS if getattr(self, name) is None]
        if missing:
            raise ValueError(f"a PROVEN program-build fact requires {', '.join(missing)}")
        if not self.evidence_refs:
            raise ValueError("a PROVEN program-build fact requires non-empty evidence_refs")
        return self


class StartRunFacts(BaseModel):
    """Bot-owned immutable and runtime facts for a proposed first run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["START"] = "START"
    strategy_instance_id: str
    proposed_run_id: str
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_account_id: str
    # First-class strictness tier (#1702): the pure admission policy branches
    # on this to relax evidence/custody-display gates for Dry Run only. Never
    # defaulted — every construction site must be explicit about which tier
    # it is admitting into.
    mode: Literal["log_only", "dry_run", "trade"]
    program_build: ProgramBuildAdmissionFact
    validation: StrategyValidationAdmissionFact
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


class TerminalEvidenceAdmissionFact(BaseModel):
    """Whether the prior run's terminal evidence is safe for Resume to touch.

    Mirrors exactly what ``BotRunEvidenceService.preserve_terminal`` will do
    at activation: an existing receipt is reused, an absent one is
    synthesized from the lifecycle summary, and either is "ready". Only a
    receipt or lifecycle projection that fails to read is unready.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["RECEIPT_READY", "SUMMARY_READY", "UNREADABLE"]
    evidence_ref: str
    explanation: str
    next_step: str | None = None


class ResumeRunFacts(BaseModel):
    """Bot-owned immutable and lifecycle facts for a proposed new run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["RESUME"] = "RESUME"
    strategy_instance_id: str
    proposed_run_id: str
    prior_run_id: str
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_account_id: str
    # Mode is immutable per instance (set at Start); Resume replays the same
    # tier the instance was created with. See StartRunFacts.mode for why this
    # is required, not defaulted.
    mode: Literal["log_only", "dry_run", "trade"]
    program_build: ProgramBuildAdmissionFact
    validation: StrategyValidationAdmissionFact
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
    terminal_evidence: TerminalEvidenceAdmissionFact


RunAdmissionFacts = StartRunFacts | ResumeRunFacts


class RunAdmissionFactAges(BaseModel):
    """Age of every authority fact at the exact admission evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    program_build: int
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
