"""HTTP contracts for Golden Search (#2696, ADR 0074).

Every temporal value on the wire is ``int64 ms UTC``; interval boundaries are
ET-midnight instants with exclusive ends. Requests accept snake_case and
camelCase (the .NET jobs passthrough) and refuse unknown keys. A plan's
*problems* are not schema errors: the request models type the plan's shape
only, and every reason a well-formed plan cannot run comes back from
``/preflight`` as a refusal in the body. Parameters travel as plain
``dict[str, Any]`` canonical points, never as a strategy's params model.

Responses refuse undeclared keys too, so a read model that drifts from this
contract fails loudly instead of being silently trimmed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.research.golden_search.declarations import KnobKind
from app.research.golden_search.evidence import CellStatus
from app.research.golden_search.exam_rules import CheckStatus, ExamOutcome
from app.research.golden_search.exposure_rules import Claim, ExposureState
from app.research.golden_search.models import CandidateKey, CommandName, StageName, StudyState
from app.research.golden_search.protocol import (
    DEFAULT_STRESS,
    MAX_BUDGET_CAP,
    ExecutionAssumptions,
    IncumbentSource,
    KnobMode,
    Method,
    SelectionPolicy,
    ZoomSettings,
)
from app.research.golden_search.zoom import StopReason
from app.research.sweep.identity import TreeState
from app.research.sweep.ranking import RankingMeasure
from app.research.walk_forward_study.verdict import VerdictLabel
from app.schemas.engine_backtest import COMMISSION_PER_ORDER_DESCRIPTION
from app.schemas.grid_search import FillModeName
from app.utils.session_anchors import MAX_TIMESTAMP_MS

InstantMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]
PresentedStatus = Literal["idle", "queued", "running", "completed", "failed", "cancelled", "interrupted"]
IDEMPOTENCY_KEY_MAX_LENGTH = 200

_POLICY = SelectionPolicy()
_ZOOM = ZoomSettings()
_EXECUTION = ExecutionAssumptions()


# ── Requests ─────────────────────────────────────────────────────────────


class _CamelTolerantModel(BaseModel):
    """Accepts camelCase (the .NET jobs passthrough) and snake_case; refuses unknown keys and non-finite numbers."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid", allow_inf_nan=False)


class GoldenSearchKnobPlanRequest(_CamelTolerantModel):
    """One declared knob: searched over ``[low, high]`` at ``step``, or held at ``fixed_value``."""

    name: str = Field(min_length=1, max_length=64)
    mode: KnobMode
    low: float
    high: float
    fixed_value: float
    step: float | None = None
    # Strict: a JSON true or 5.5 is refused here rather than coerced to a score (#2813 review).
    importance: int | None = Field(None, strict=True, description="1-10, higher searched first; null on a plan that keeps its own knob order.")


class GoldenSearchSelectionPolicyRequest(_CamelTolerantModel):
    objective: RankingMeasure = _POLICY.objective
    min_trades: int | None = Field(_POLICY.min_trades, description="A fixed floor; null on a plan with an expected trade frequency.")
    max_drawdown_ceiling: float = Field(_POLICY.max_drawdown_ceiling, description="A fraction of peak equity, in (0, 1].")
    require_positive_net: bool = _POLICY.require_positive_net


class GoldenSearchZoomSettingsRequest(_CamelTolerantModel):
    points: int = _ZOOM.points
    refinements: int = _ZOOM.refinements
    passes: int = _ZOOM.passes


class GoldenSearchExecutionRequest(_CamelTolerantModel):
    fill_mode: FillModeName = "decision_minute_open"
    commission_per_order: float = Field(_EXECUTION.commission_per_order, description=COMMISSION_PER_ORDER_DESCRIPTION)
    slippage_per_share: float = _EXECUTION.slippage_per_share
    initial_cash: float = _EXECUTION.initial_cash


class GoldenSearchStressScenarioRequest(_CamelTolerantModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    slippage_add: float = 0.0
    commission_add: float = 0.0
    fill_mode: FillModeName | None = None


def _default_stress() -> list[GoldenSearchStressScenarioRequest]:
    return [GoldenSearchStressScenarioRequest.model_validate(asdict(scenario)) for scenario in DEFAULT_STRESS]


class GoldenSearchIncumbentRequest(_CamelTolerantModel):
    source: IncumbentSource
    qualification_id: str | None
    params: dict[str, Any]


class GoldenSearchProtocolRequest(_CamelTolerantModel):
    """The plan, field for field ``GoldenSearchProtocol``; ``seed`` omitted or null starts from the incumbent."""

    strategy_key: str = Field(min_length=1, max_length=128)
    # Upper-cased here and again by the service; the plan review refuses a symbol that is not a ticker.
    symbol: str = Field(min_length=1, max_length=16)
    method: Method
    knobs: list[GoldenSearchKnobPlanRequest] = Field(max_length=64, description="Every declared knob, in search order.")
    seed: dict[str, Any] | None = None
    incumbent: GoldenSearchIncumbentRequest
    development_start_ms: InstantMs
    development_end_ms: InstantMs
    final_start_ms: InstantMs
    final_end_ms: InstantMs
    policy: GoldenSearchSelectionPolicyRequest = Field(default_factory=GoldenSearchSelectionPolicyRequest)
    zoom: GoldenSearchZoomSettingsRequest = Field(default_factory=GoldenSearchZoomSettingsRequest)
    training_months: int = 6
    test_months: int = 2
    recent_window: bool = True
    pair_audits: list[tuple[str, str]] = Field(default_factory=list, max_length=64)
    neighbor_audit: bool = True
    stress: list[GoldenSearchStressScenarioRequest] = Field(default_factory=_default_stress, max_length=16)
    execution: GoldenSearchExecutionRequest = Field(default_factory=GoldenSearchExecutionRequest)
    exam_min_trades: int | None = Field(30, description="A fixed final-test floor; null on a plan with an expected trade frequency.")
    budget_cap: int = MAX_BUDGET_CAP
    expected_trades_per_year: int | None = Field(
        None,
        strict=True,
        description="Completed trades per trading year; each window's minimum scales with its trading sessions. Null keeps the fixed floors.",
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    def as_plan(self) -> dict[str, Any]:
        """The snake_case plan the study service reads."""
        return self.model_dump(mode="json")


class GoldenSearchCreateStudyRequest(_CamelTolerantModel):
    protocol: GoldenSearchProtocolRequest
    idempotency_key: str = Field(min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)
    run_research: bool = Field(
        False, description="Also start Search and run on through Test over time, pausing at Compare; the answer carries the dispatch."
    )


class GoldenSearchRevisePayload(_CamelTolerantModel):
    protocol: GoldenSearchProtocolRequest


class GoldenSearchCommandRequest(_CamelTolerantModel):
    """One lifecycle command. The study service validates each command's payload and refuses it with a code.

    ``revise`` carries a whole plan, so its payload is read through the same
    schema as a lock and normalized before the idempotency digest is taken.
    """

    command: CommandName
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _revise_carries_a_plan(self) -> GoldenSearchCommandRequest:
        if self.command == "revise":
            revised = GoldenSearchRevisePayload.model_validate(self.payload)
            self.payload = {"protocol": revised.protocol.as_plan()}
        return self


class GoldenSearchJobRequest(_CamelTolerantModel):
    """Body of POST /api/jobs-internal/golden-search: the dispatch a guarded command returned, plus the minted job id."""

    job_id: str = Field(min_length=1, max_length=128)
    study_id: str = Field(min_length=1, max_length=64)
    stage_token: str = Field(min_length=1, max_length=128)


# ── Responses ────────────────────────────────────────────────────────────


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldenSearchMetrics(_Wire):
    status: Literal["completed", "failed"]
    total_trades: int
    net_profit: float | None
    total_return_pct: float | None = Field(description="A fraction, as the engine reports it.")
    sharpe_ratio: float | None
    max_drawdown_pct: float | None = Field(description="A fraction of peak equity, as the engine reports it.")
    win_rate: float | None
    error: str | None


class GoldenSearchWindow(_Wire):
    start_ms: InstantMs
    end_ms: InstantMs


class GoldenSearchCapabilityKnob(_Wire):
    name: str
    label: str
    unit: str
    kind: KnobKind
    domain_low: float
    domain_high: float
    quantum: float
    default_low: float
    default_high: float
    neighbor_step: float
    default_step: float
    searchable_by_default: bool
    warmup_dependent: bool
    default_value: int | float
    note: str


class GoldenSearchFixedControl(_Wire):
    label: str
    value: str
    reason: str


class GoldenSearchConstraint(_Wire):
    left: str
    op: Literal["<"]
    right: str
    message: str


class GoldenSearchImportanceScale(_Wire):
    """How important each knob is to its owner; the more important, the earlier the search moves it."""

    low: int
    high: int
    default: int


class GoldenSearchCapability(_Wire):
    strategy_key: str
    display_name: str
    available: bool
    reason: str | None
    knobs: list[GoldenSearchCapabilityKnob]
    fixed: list[GoldenSearchFixedControl]
    constraints: list[GoldenSearchConstraint]
    default_pair_audits: list[tuple[str, str]]
    default_expected_trades_per_year: int = Field(description="The expected trade frequency a new plan starts with, in completed trades per trading year.")
    importance: GoldenSearchImportanceScale


# The frozen plan as stored and echoed (snake_case only).


class GoldenSearchKnobPlan(_Wire):
    name: str
    mode: KnobMode
    low: float
    high: float
    fixed_value: float
    step: float | None
    importance: int | None = None


class GoldenSearchSelectionPolicy(_Wire):
    objective: RankingMeasure
    min_trades: int | None
    max_drawdown_ceiling: float
    require_positive_net: bool


class GoldenSearchZoomSettings(_Wire):
    points: int
    refinements: int
    passes: int


class GoldenSearchExecution(_Wire):
    fill_mode: FillModeName
    commission_per_order: float
    slippage_per_share: float
    initial_cash: float


class GoldenSearchStressScenario(_Wire):
    key: str
    label: str
    slippage_add: float
    commission_add: float
    fill_mode: FillModeName | None


class GoldenSearchIncumbent(_Wire):
    source: IncumbentSource
    qualification_id: str | None
    params: dict[str, Any]


class GoldenSearchProtocol(_Wire):
    strategy_key: str
    symbol: str
    method: Method
    knobs: list[GoldenSearchKnobPlan]
    seed: dict[str, Any]
    incumbent: GoldenSearchIncumbent
    policy: GoldenSearchSelectionPolicy
    zoom: GoldenSearchZoomSettings
    development_start_ms: InstantMs
    development_end_ms: InstantMs
    final_start_ms: InstantMs
    final_end_ms: InstantMs
    training_months: int
    test_months: int
    recent_window: bool
    pair_audits: list[tuple[str, str]]
    neighbor_audit: bool
    stress: list[GoldenSearchStressScenario]
    execution: GoldenSearchExecution
    exam_min_trades: int | None
    budget_cap: int
    expected_trades_per_year: int | None = None


class GoldenSearchExposure(_Wire):
    state: ExposureState
    ledger_overlaps: int
    outside_activity_overlaps: int
    explanation: str


class GoldenSearchDefaults(GoldenSearchProtocol):
    """A complete starting plan with server-computed intervals, the incumbent's name and the final interval's exposure."""

    final_months: int = Field(description="The final interval's length in whole months, as laid out.")
    final_sessions_cut: int = Field(
        description="Trailing scheduled sessions the final interval leaves out because the lake has not reached them yet; 0 for whole months."
    )
    incumbent_label: str
    incumbent_sentence: str = Field(description="The incumbent's settings at a glance, e.g. 'Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars'.")
    exposure: GoldenSearchExposure


class GoldenSearchProtocolRefusal(_Wire):
    code: str
    field: str | None
    message: str


class GoldenSearchStageEstimate(_Wire):
    stage: str
    label: str
    max_evaluations: int


class GoldenSearchEstimate(_Wire):
    stages: list[GoldenSearchStageEstimate]
    total_max: int
    reserved_for_exam_and_proof: int
    budget_cap: int
    serial_seconds_low: float = Field(description="An estimate, not a promise.")
    serial_seconds_high: float = Field(description="An estimate, not a promise.")


class GoldenSearchFold(_Wire):
    fold_index: int
    train_start_ms: InstantMs
    train_end_ms: InstantMs
    test_start_ms: InstantMs
    test_end_ms: InstantMs


class GoldenSearchRunUp(_Wire):
    required_samples: int
    run_up_sessions: int
    data_start_ms: InstantMs


class GoldenSearchKnobValues(_Wire):
    name: str
    values: int | None = Field(description="Settings the knob can take: 1 when held, its range's size at its step when searched, null when that range is not valid.")


class GoldenSearchActivityYear(_Wire):
    year: int
    selected_sessions: int
    year_sessions: int


class GoldenSearchActivityWindow(_Wire):
    key: str
    label: str
    start_ms: InstantMs
    end_ms: InstantMs
    trading_sessions: int
    minimum_trades: int
    years: list[GoldenSearchActivityYear]


class GoldenSearchActivity(_Wire):
    expected_trades_per_year: int
    windows: list[GoldenSearchActivityWindow]


class GoldenSearchPreflight(_Wire):
    """A plan's review: refusals are data in a 200, never a 400."""

    refusals: list[GoldenSearchProtocolRefusal]
    knob_values: list[GoldenSearchKnobValues]
    estimate: GoldenSearchEstimate | None
    folds: list[GoldenSearchFold]
    exposure: GoldenSearchExposure | None
    run_up: GoldenSearchRunUp | None
    activity: GoldenSearchActivity | None = None


# Study summary and detail.


class GoldenSearchStudySummary(_Wire):
    id: str
    parent_study_id: str | None
    strategy_key: str
    symbol: str
    state: StudyState
    presented_status: PresentedStatus
    revision: int
    created_at_ms: InstantMs
    updated_at_ms: InstantMs
    protocol_hash: str
    method: Method
    consumed_evaluations: int
    budget_cap: int
    cache_hits: int
    invalid_points: int
    incomplete: bool
    failure_reason: str | None
    hidden: bool
    run_to_compare: bool = Field(description="Run research is on: Search hands on to Test over time and the study pauses at Compare.")
    exposure_claim: Claim | None
    exam_outcome: ExamOutcome | None
    qualification_id: str | None


class GoldenSearchCodeIdentity(_Wire):
    git_revision: str
    tree_state: TreeState


class GoldenSearchReceiptSummary(_Wire):
    data_start_ms: InstantMs
    development_start_ms: InstantMs
    development_end_ms: InstantMs
    final_start_ms: InstantMs
    final_end_ms: InstantMs
    run_up_sessions: int
    snapshot_digest: str
    code: GoldenSearchCodeIdentity
    program_version: str | None


class GoldenSearchGuidance(_Wire):
    headline: str
    detail: str


class GoldenSearchProgress(_Wire):
    stage: StageName
    completed: int
    total_max: int


class GoldenSearchDispatchPayload(_Wire):
    study_id: str
    stage_token: str


class GoldenSearchDispatch(_Wire):
    """What the client hands the jobs boundary to start the stage a command authorized."""

    job_type: Literal["golden_search"]
    payload: GoldenSearchDispatchPayload


class GoldenSearchDecision(_Wire):
    kind: str
    note: str
    at_ms: InstantMs


class GoldenSearchScope(_Wire):
    development_label_start_ms: InstantMs
    development_end_ms: InstantMs
    final_start_ms: InstantMs
    final_end_ms: InstantMs
    final_state: Literal["locked", "opened_once"]
    capital: float
    costs_sentence: str
    data_source: str


class GoldenSearchZoomRound(_Wire):
    pass_index: int
    knob: str
    round_index: int
    low: float
    high: float
    values: list[float]
    invalid: list[tuple[float, str]] = Field(description="Constraint-skipped values, never evaluated: [value, reason].")
    results: list[tuple[float, str | None]] = Field(description="[value, ineligibility code or null when eligible].")
    objectives: list[tuple[float, float | None]]
    chosen: float
    moved: bool
    current_before: float
    quantization_limit: bool


class GoldenSearchKnobSummary(_Wire):
    knob: str
    label: str
    unit: str
    start_value: int | float
    retained_value: int | float
    moved: bool
    stop_reason: StopReason
    stop_explanation: str


class GoldenSearchProcedureCounts(_Wire):
    evaluated: int
    cached: int
    invalid: int


class GoldenSearchPairCell(_Wire):
    x: float
    y: float
    status: CellStatus
    metrics: GoldenSearchMetrics | None
    reason: str | None


class GoldenSearchPairMap(_Wire):
    """A predeclared pair's landscape: rows are ``y_knob``, columns ``x_knob``, cells row-major."""

    x_knob: str
    y_knob: str
    x_values: list[float]
    y_values: list[float]
    cells: list[GoldenSearchPairCell]


class GoldenSearchProcedureView(_Wire):
    winner: dict[str, Any]
    winner_hash: str
    winner_metrics: GoldenSearchMetrics | None
    stop_reason: StopReason
    stop_explanation: str
    rounds: list[GoldenSearchZoomRound]
    edge_hits: list[str]
    evaluations: int
    incomplete: bool
    knob_summary: list[GoldenSearchKnobSummary]
    counts: GoldenSearchProcedureCounts
    passes_completed: int
    window: GoldenSearchWindow


class GoldenSearchSearchView(GoldenSearchProcedureView):
    """The all-period procedure, with the pair landscapes centered on its winner."""

    pair_maps: list[GoldenSearchPairMap]
    pair_maps_incomplete: bool


class GoldenSearchVerdict(_Wire):
    label: VerdictLabel
    reason: str
    successful_folds: int
    defined_folds: int
    study_retention: float | None
    median_test_sharpe: float | None
    oos_trade_count: int
    based_on: str
    retention_threshold: float


class GoldenSearchValidationFold(_Wire):
    fold_index: int
    train_start_ms: InstantMs
    train_end_ms: InstantMs
    test_start_ms: InstantMs
    test_end_ms: InstantMs
    status: Literal["pending", "completed", "failed"]
    winner: dict[str, Any] | None
    winner_hash: str | None
    train_metrics: GoldenSearchMetrics | None
    test_metrics: GoldenSearchMetrics | None
    incumbent_test_metrics: GoldenSearchMetrics | None
    failure_reason: str | None
    failure_code: str | None


class GoldenSearchLinkedReturn(_Wire):
    fold_index: int
    test_end_ms: InstantMs
    linked_return: float | None = Field(description="Growth of 1 linked across test folds, minus 1; null once a fold is missing.")
    fold_missing: bool = Field(description="This fold has no test return of its own (a null linked_return after it is the broken line).")


class GoldenSearchSummaryPills(_Wire):
    judged: str
    test_trades: int
    median_retention: float | None


class GoldenSearchValidationView(_Wire):
    folds: list[GoldenSearchValidationFold]
    verdict: GoldenSearchVerdict | None
    linked: list[GoldenSearchLinkedReturn]
    incumbent_linked: list[GoldenSearchLinkedReturn]
    summary_pills: GoldenSearchSummaryPills
    explanation: str
    incomplete: bool


class GoldenSearchNeighborRow(_Wire):
    value: float
    status: CellStatus
    metrics: GoldenSearchMetrics | None
    reason: str | None
    step: Literal[-1, 0, 1] = Field(description="The row's step from the candidate on this knob: -1 below, 0 the candidate itself, +1 above.")
    return_change: float | None = Field(
        description="This neighbor's net return less the candidate's, a fraction of starting capital; null for the candidate's own row or when either run has no completed result."
    )


class GoldenSearchNeighborhood(_Wire):
    knob: str
    rows: list[GoldenSearchNeighborRow]
    one_sided: bool


class GoldenSearchStressResult(_Wire):
    scenario: str
    label: str
    metrics: GoldenSearchMetrics | None
    return_change: float | None = Field(
        description="This stressed run's net return less the unstressed development run's, a fraction of starting capital; null when either run has no completed result."
    )


class GoldenSearchStressTally(_Wire):
    in_profit: int = Field(description="Stress runs that completed with a net profit above zero.")
    recorded: int = Field(description="Stress runs that completed with a recorded net profit, of any sign.")
    scenarios: int = Field(description="Stress scenarios the plan scheduled.")


class GoldenSearchBestMonth(_Wire):
    month_start_ms: InstantMs = Field(description="The month's start at ET midnight.")
    net_profit: float


class GoldenSearchRankedTrade(_Wire):
    entry_ms: InstantMs
    exit_ms: InstantMs
    net_profit: float = Field(description="The trade's P&L less its entry and exit commission.")


class GoldenSearchConcentrationMeasured(_Wire):
    """How much of the development result rests on its best month or its best trades (#2815). It never gates."""

    status: Literal["meets", "concern"] = Field(description="Concern when either result without its best is $0 or less.")
    net_profit: float = Field(description="The development run's net profit.")
    trades: int
    best_month: GoldenSearchBestMonth
    without_best_month: float = Field(description="Net profit less the best month's, to the cent.")
    best_trades: list[GoldenSearchRankedTrade] = Field(description="The best 5% of the trades, rounded up, best first.")
    best_trades_net_profit: float = Field(description="The best trades' net profit together.")
    without_best_trades: float = Field(description="Net profit less the best trades', to the cent.")


class GoldenSearchConcentrationMissing(_Wire):
    status: Literal["missing"]
    reason: str = Field(description="Why nothing was measured, including evidence recorded before the evidence stage measured it.")


GoldenSearchConcentration = Annotated[GoldenSearchConcentrationMeasured | GoldenSearchConcentrationMissing, Field(discriminator="status")]


class GoldenSearchCandidateGuidance(_Wire):
    title: str
    text: str


class GoldenSearchFinding(_Wire):
    code: str
    text: str


class GoldenSearchEvidenceCandidate(_Wire):
    key: CandidateKey
    label: str
    point: dict[str, Any]
    point_hash: str
    same_as: list[CandidateKey]
    development_metrics: GoldenSearchMetrics | None
    eligible: bool
    ineligibility: str | None
    neighbors: list[GoldenSearchNeighborhood]
    stress: list[GoldenSearchStressResult]
    trades_per_year: float | None = Field(
        description="Development trades per trading year of the development window; null when the development run has no completed result."
    )
    stress_tally: GoldenSearchStressTally
    concentration: GoldenSearchConcentration
    edge_hits: list[str]
    guidance: GoldenSearchCandidateGuidance
    flags: list[GoldenSearchFinding]
    params_sentence: str
    fixed_sentence: str
    exam_eligible: bool


class GoldenSearchRecommendation(_Wire):
    headline: str
    findings: list[GoldenSearchFinding]


class GoldenSearchCosts(_Wire):
    fill_mode: FillModeName
    commission_per_order: float
    slippage_per_share: float


class GoldenSearchEvidenceScope(_Wire):
    window: GoldenSearchWindow
    capital: float
    costs: GoldenSearchCosts


class GoldenSearchEvidenceView(_Wire):
    candidates: list[GoldenSearchEvidenceCandidate]
    pair_maps: list[GoldenSearchPairMap]
    recommendation: GoldenSearchRecommendation
    scope: GoldenSearchEvidenceScope
    incomplete: bool


class GoldenSearchExamCheck(_Wire):
    code: str
    label: str
    status: CheckStatus
    detail: str


class GoldenSearchExamView(_Wire):
    candidate_key: CandidateKey
    candidate_point: dict[str, Any]
    window: GoldenSearchWindow
    claim: Claim
    exposure_state: ExposureState
    outcome: ExamOutcome | None
    checks: list[GoldenSearchExamCheck]
    retention: float | None = Field(description="Descriptive only, never a check.")
    candidate_metrics: GoldenSearchMetrics | None
    incumbent_metrics: GoldenSearchMetrics | None
    weakness: list[GoldenSearchFinding] = Field(
        description="What approving needs the owner to acknowledge as weak; empty when the evidence meets the rules."
    )


class GoldenSearchDeployHandoff(_Wire):
    program_key: str
    symbol: str
    parameters: dict[str, Any] = Field(description="The canonical point without symbol.")
    program_version: str | None


class GoldenSearchQualificationView(_Wire):
    status: Literal["pending", "ready", "failed"]
    qualification_id: str | None
    failure_reason: str | None
    deploy: GoldenSearchDeployHandoff | None


class GoldenSearchResults(_Wire):
    search: GoldenSearchSearchView | None
    recent: GoldenSearchProcedureView | None
    validation: GoldenSearchValidationView | None
    evidence: GoldenSearchEvidenceView | None
    exam: GoldenSearchExamView | None
    qualification: GoldenSearchQualificationView | None


class GoldenSearchSummaryLink(_Wire):
    kind: Literal["tab", "chart", "step"]
    target: str = Field(
        description="An evidence tab of the Compare step (trades, months), a Compare chart (neighbor-tornado, cost-stress) or a study step (search, test, decision)."
    )


class GoldenSearchSummaryRow(_Wire):
    """One kind of evidence for a candidate; a result the study did not record is ``missing``, never ``meets``."""

    key: str
    label: str
    status: Literal["meets", "concern", "missing"]
    text: str
    link: GoldenSearchSummaryLink


class GoldenSearchDecisionSummary(_Wire):
    candidate_key: CandidateKey
    rows: list[GoldenSearchSummaryRow]


class GoldenSearchStudyDetail(GoldenSearchStudySummary):
    protocol: GoldenSearchProtocol
    receipt: GoldenSearchReceiptSummary
    activity: GoldenSearchActivity | None = None
    decision_summaries: list[GoldenSearchDecisionSummary] = Field(
        description="What the research supports for each Compare candidate (#2811); empty before the evidence exists."
    )
    permitted_actions: list[CommandName]
    action_refusals: dict[CommandName, str]
    guidance: GoldenSearchGuidance
    progress: GoldenSearchProgress | None
    dispatch: GoldenSearchDispatch | None
    results: GoldenSearchResults
    decision: GoldenSearchDecision | None
    candidate_key: CandidateKey | None
    exam_locked: bool
    scope: GoldenSearchScope
    exposure_preview: GoldenSearchExposure | None = Field(
        description="What opening the final test would record, while a candidate is chosen; null otherwise."
    )


# Reads beside the study.


class GoldenSearchEvaluationRow(_Wire):
    evaluation_key: str
    point_hash: str
    point: dict[str, Any]
    window_start_ms: InstantMs
    window_end_ms: InstantMs
    scenario: str
    detail: bool
    stage: str
    fold_index: int | None
    status: Literal["pending", "completed", "failed"]
    attempt: int
    retries: int
    total_trades: int | None
    net_profit: float | None
    total_return_pct: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float | None
    win_rate: float | None
    error: str | None
    created_at_ms: InstantMs
    completed_at_ms: InstantMs | None


class GoldenSearchEvaluationPage(_Wire):
    total: int
    page: int
    page_size: int
    rows: list[GoldenSearchEvaluationRow]


class GoldenSearchCumulativeReturnPoint(_Wire):
    ms: InstantMs
    value: float = Field(description="A fraction of starting capital.")


class GoldenSearchEquityPoint(_Wire):
    ms: InstantMs
    equity: float


class GoldenSearchDrawdownPoint(_Wire):
    ms: InstantMs
    drawdown: float


class GoldenSearchMonthlyResult(_Wire):
    month_start_ms: InstantMs
    year: int = Field(description="The month's ET calendar year.")
    month: int = Field(ge=1, le=12, description="The month's ET calendar month, 1 for January.")
    net_profit: float
    return_fraction: float | None
    trades: int


class GoldenSearchTradeRecord(_Wire):
    entry_ms: InstantMs
    exit_ms: InstantMs
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float = Field(description="Price change times filled quantity, before fees.")
    net_profit: float = Field(description="P&L before fees less the entry and exit commission.")
    running_net_profit: float = Field(description="Net profit of this trade and every trade that exited before it.")
    bars_held: int = Field(description="Decision bars that closed after the entry and by the exit, on the trading calendar.")
    entry_rsi: float | None = Field(description="RSI the strategy recorded when it decided to enter; null when it recorded none.")
    exit_kind: Literal["strategy", "window_end"]
    exit_reason: str


class GoldenSearchHistogramBin(_Wire):
    low: float
    high: float = Field(description="Exclusive, except for a single bin of trades that all net the same.")
    trades: int
    wins: int = Field(description="Trades in the bin that netted more than $0.")
    losses: int = Field(description="Trades in the bin that netted less than $0.")


class GoldenSearchHistogram(_Wire):
    bin_width: float = Field(description="0 when every trade nets the same.")
    bins: list[GoldenSearchHistogramBin] = Field(description="From the lowest to the highest, with $0 as an edge; empty bins between are kept.")


class GoldenSearchRsiBand(_Wire):
    low: float
    high: float = Field(description="Exclusive, except for the last band, which ends at the upper gate.")
    trades: int
    mean_net_profit: float | None = Field(description="Null for a band no trade entered in.")


class GoldenSearchEntryRsiMeasured(_Wire):
    status: Literal["measured"]
    gate_low: float
    gate_high: float
    bands: list[GoldenSearchRsiBand]
    unbanded: int = Field(description="Trades with no RSI recorded at entry or one outside the gates.")


class GoldenSearchEntryRsiMissing(_Wire):
    status: Literal["missing"]
    reason: str


GoldenSearchEntryRsi = Annotated[GoldenSearchEntryRsiMeasured | GoldenSearchEntryRsiMissing, Field(discriminator="status")]


class GoldenSearchEntryTimeCell(_Wire):
    weekday: int = Field(description="Index into the weekdays.")
    half_hour: int = Field(description="Index into the half hours.")
    trades: int
    mean_net_profit: float
    total_net_profit: float
    too_few: bool = Field(description="Fewer trades than the minimum, so its average means little.")


class GoldenSearchEntryTimes(_Wire):
    weekdays: list[str] = Field(description="The rows: ET weekdays the sessions hold, Monday first.")
    half_hours: list[str] = Field(description="The columns: the ET half hours the sessions cover, as HH:MM.")
    min_trades: int
    cells: list[GoldenSearchEntryTimeCell] = Field(description="Only the cells a trade entered in.")


class GoldenSearchTradeChartsMeasured(_Wire):
    status: Literal["measured"]
    bar_span_ms: int = Field(description="The strategy's decision bar, in ms; a day or longer means one bar per session.")
    trades: list[GoldenSearchTradeRecord] = Field(description="In exit order.")
    histogram: GoldenSearchHistogram
    entry_rsi: GoldenSearchEntryRsi
    entry_times: GoldenSearchEntryTimes


class GoldenSearchTradeChartsMissing(_Wire):
    status: Literal["missing"]
    reason: str


GoldenSearchTradeCharts = Annotated[GoldenSearchTradeChartsMeasured | GoldenSearchTradeChartsMissing, Field(discriminator="status")]


class GoldenSearchCurvePoint(_Wire):
    trades: int = Field(description="Trades counted so far, best first.")
    share_of_trades: float = Field(description="A fraction of all the run's trades.")
    share_of_profit: float = Field(description="Their running net profit as a fraction of the run's.")
    net_profit: float = Field(description="Their running net profit.")


class GoldenSearchConcentrationCurve(_Wire):
    points: list[GoldenSearchCurvePoint] = Field(description="From no trades to every trade; empty when no curve is drawn.")
    best_count: int | None = Field(description="How many trades make the best 5%, rounded up; null when no curve is drawn.")
    reason: str | None = Field(description="Why no curve is drawn; null when it is.")


class GoldenSearchRunDetail(_Wire):
    window: GoldenSearchWindow
    metrics: GoldenSearchMetrics
    cumulative_return: list[GoldenSearchCumulativeReturnPoint]
    daily_equity: list[GoldenSearchEquityPoint]
    drawdown: list[GoldenSearchDrawdownPoint]
    monthly: list[GoldenSearchMonthlyResult]
    concentration_curve: GoldenSearchConcentrationCurve | None = Field(description="The development run's; null on a final-test run.")
    trade_charts: GoldenSearchTradeCharts | None = Field(description="The development run's; null on a final-test run.")


class GoldenSearchCandidateDetail(_Wire):
    candidate_key: CandidateKey
    point: dict[str, Any]
    development: GoldenSearchRunDetail | None
    exam: GoldenSearchRunDetail | None


class GoldenSearchJobAccepted(_Wire):
    job_id: str
    study_id: str
    status: Literal["queued"]


# Refusals.


class GoldenSearchRefusalDetail(_Wire):
    """Why a request was refused, under one shape for every refusal status (400, 404, 409, 503)."""

    code: str
    message: str
    field: str | None
    refusals: list[GoldenSearchProtocolRefusal] = Field(description="Every plan refusal when a lock is refused for more than one reason.")
    # Typed loosely here so the documented error body does not fork the StudyDetail schema; it is
    # built from a validated GoldenSearchStudyDetail.
    study: dict[str, Any] | None = Field(description="On a 409 about a study: its GoldenSearchStudyDetail as it now stands.")


class GoldenSearchRefusalBody(_Wire):
    detail: GoldenSearchRefusalDetail
