"""Options Golden Search HTTP contracts (#3332), with exact money and UTC-ms anchors."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.options_backtest.setup import OptionsBacktestSetup
from app.research.options_golden_search.protocol import CostScenario, Knob, Limits, ZoomSettings
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SearchPolicyRequest(Model):
    max_drawdown_percent: Decimal = Field(default=Decimal(20), ge=0, le=100)
    min_sharpe: float = 1
    min_activity_per_year: int = Field(default=50, ge=1)
    min_daily_observations: int = Field(default=30, ge=2)
    min_forward_sessions: int = Field(default=30, ge=1)
    min_final_sessions: int = Field(default=30, ge=1)
    max_recovery_sessions: int | None = Field(default=None, ge=1)
    risk_free_daily_return: float = 0
    stress_gate: bool = False


class History(Model):
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS, strict=True)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS, strict=True)
    train_months: int = Field(default=6, ge=1, le=120)
    test_months: int = Field(default=2, ge=1, le=60)
    final_months: int = Field(default=6, ge=1, le=60)


class PlanRequest(Model):
    setup_name: str = Field(min_length=1, max_length=80)
    expected_setup_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_study_id: str | None = Field(default=None, pattern=r"^os_[a-f0-9]{40}$")
    knobs: tuple[Knob, ...]
    method: Literal["zoom", "grid"] = "zoom"
    policy: SearchPolicyRequest = Field(default_factory=SearchPolicyRequest)
    history: History
    budget: Limits = Field(default_factory=Limits)
    finalist_count: int = Field(default=3, ge=1, le=10)
    stress: tuple[CostScenario, ...] = ()
    zoom: ZoomSettings = Field(default_factory=ZoomSettings)
    pair_audits: tuple[tuple[str, str], ...] = ()


class LockRequest(Model):
    request: PlanRequest
    preflight_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class Command(Model):
    command: Literal["run_research", "cancel", "resume", "open_final", "approve", "approve_exception", "keep_original"]
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    candidate_hashes: tuple[str, ...] | None = Field(default=None, max_length=11)
    candidate_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    comment: str = Field(default="", max_length=10000)
    waived_gate_ids: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = Field(default=(), max_length=10000)


class JobRequest(Model):
    study_id: str = Field(min_length=1, max_length=64)
    stage_token: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class JobAccepted(Model):
    job_id: str
    study_id: str
    status: Literal["queued"] = "queued"


class KnobDeclaration(Model):
    path: str
    label: str
    leg_id: str | None
    current_value: JsonValue
    value_type: Literal["integer", "decimal", "boolean", "category"]
    legal_values: list[JsonValue]
    min: str | None = None
    max: str | None = None
    step: str | None = None
    help: str


class HistoryBounds(Model):
    start_ms: int | None
    end_ms: int | None


class PlanDefaults(Model):
    version: str
    rationale: str
    policy: SearchPolicyRequest
    train_months: int = 6
    test_months: int = 2
    final_months: int = 6
    evaluation_cap: int = 5000
    finalist_count: int = 3


class Declarations(Model):
    setup_name: str
    setup: OptionsBacktestSetup
    setup_hash: str
    knobs: list[KnobDeclaration]
    history_bounds: HistoryBounds
    defaults: PlanDefaults


class WindowReview(Model):
    label: str
    start_ms: int
    end_ms: int
    scheduled_sessions: int
    required_traded_sessions: int


class Preflight(Model):
    preflight_hash: str
    can_lock: bool
    blockers: list[str]
    warnings: list[str]
    drawdown_limit_usd: str
    combination_count: int | None
    legal_combination_count: int | None
    invalid_combination_count: int | None
    required_evaluations: int
    reserved_evaluations: int
    estimated_setup_sessions: int
    mark_coverage: str
    exposure: str
    identity: str
    windows: list[WindowReview]


class GateOutcome(Model):
    gate_id: str
    status: str
    actual: str | None
    required: str | None
    reason: str
    overridable: bool = True


class EvidenceItem(Model):
    label: str
    value: str


class MonthResult(Model):
    month_ms: int
    net_profit_usd: str
    completed_sessions: int


class EquityPoint(Model):
    timestamp_ms: int
    equity_usd: str
    drawdown_usd: str


class LegAttempt(Model):
    leg_id: str
    attempt: int
    contract: str
    side: str
    contracts: int
    entry_ms: int
    exit_ms: int
    entry_price_usd: str
    exit_price_usd: str
    entry_fee_usd: str
    exit_fee_usd: str
    net_profit_usd: str
    exit_reason: str


class SessionResult(Model):
    session_open_ms: int
    net_profit_usd: str
    status: str
    detail: str
    attempts: list[LegAttempt] = Field(default_factory=list)


class Candidate(Model):
    candidate_hash: str
    label: str
    is_original: bool
    verdict: Literal["meets_rules", "does_not_meet_rules", "not_enough_evidence", "could_not_evaluate"]
    net_profit_usd: str | None
    drawdown_usd: str | None
    drawdown_fraction: str | None
    drawdown_percent: str | None
    sharpe: float | None
    completed_sessions: int | None
    recovery: str
    gate_outcomes: list[GateOutcome]
    evidence: list[EvidenceItem]
    monthly: list[MonthResult]
    equity: list[EquityPoint]
    sessions: list[SessionResult]


class Progress(Model):
    stage: str
    completed: int = 0
    queued: int = 0
    running: int = 0
    cache_hits: int = 0
    execution_attempts: int = 0
    spent: int = 0
    reserved: int = 0
    capacity_message: str = "Width one; broader capacity requires qualification."


class Exposure(Model):
    classification: str
    explanation: str


class Dispatch(Model):
    study_id: str
    stage_token: str


class StudySummary(Model):
    id: str
    setup_name: str
    underlying: str
    phase: str
    status: str
    updated_at_ms: int


class ApprovalReview(Model):
    failures: list[GateOutcome]
    blocked_reason: str | None


class StudyDetail(Model):
    id: str
    revision: int
    phase: str
    status: str
    protocol_hash: str
    setup_name: str
    underlying: str
    progress: Progress
    candidates: list[Candidate]
    final_results: list[Candidate]
    gate_outcomes: list[GateOutcome]
    exposure: Exposure
    allowed_commands: list[str]
    dispatch: Dispatch | None
    failure_reason: str | None
    qualification_id: str | None
    qualification_status: str | None
    protocol_summary: list[EvidenceItem] = Field(default_factory=list)
    approval_reviews: dict[str, ApprovalReview] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> StudyDetail:
        from app.research.options_golden_search.projections import approval_reviews

        fields = {name: record.get(name, [] if name == "protocol_summary" else None) for name in cls.model_fields if name != "approval_reviews"}
        fields["approval_reviews"] = approval_reviews(record)
        return cls.model_validate(fields)
