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
from app.research.golden_search.run_curves import LineRole, RunGroup, RunSort
from app.research.golden_search.zoom import StopReason
from app.research.sweep.identity import TreeState
from app.research.sweep.ranking import RankingMeasure
from app.research.walk_forward_study.verdict import VerdictLabel
from app.schemas.engine_backtest import COMMISSION_PER_ORDER_DESCRIPTION
from app.schemas.grid_search import FillModeName
from app.schemas.session_curves import SessionCurve, SessionsMs
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
    range_note: str | None = Field(
        default=None,
        description="Laid out inside a picked range: which of its sessions the plan leaves out and why, and a final test that starts before its whole months. Null when neither, or when no range was given.",
    )
    incumbent_label: str
    incumbent_sentence: str = Field(description="The incumbent's settings at a glance, e.g. 'Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars'.")
    exposure: GoldenSearchExposure


class GoldenSearchBackfillRemedy(_Wire):
    """The lake backfill that fetches a refusal's missing sessions; a client adds only what a `DataRunSpec` needs besides."""

    symbol: str
    price_adjustment_mode: Literal["raw", "polygon_split_adjusted"]
    start_trading_date_ms: InstantMs = Field(description="The first missing session the provider still serves, calendar-anchored as a `DataRunSpec` bound is.")
    end_trading_date_ms: InstantMs = Field(description="The last such session, anchored the same way; inclusive.")


class GoldenSearchProtocolRefusal(_Wire):
    code: str
    field: str | None
    message: str
    nearest_valid_ms: list[InstantMs] = Field(
        default_factory=list, description="The nearest instants `field` would accept instead, when the server can name them; empty otherwise."
    )
    backfill: GoldenSearchBackfillRemedy | None = Field(
        default=None,
        description="The backfill that fetches the sessions the refusal names as missing; null when it names none, or the provider no longer serves them.",
    )


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


class GoldenSearchPlanWindow(_Wire):
    key: str
    label: str
    kind: Literal["run_up", "development", "recent", "training", "test", "forward", "final"]
    start_ms: InstantMs
    end_ms: InstantMs = Field(description="Exclusive.")
    sessions: int = Field(description="The canonical calendar's sessions in the window.")
    minimum_trades: int | None = Field(description="The trades the window must reach; null for the run-up and a single fold's test, which have none.")


class GoldenSearchPlanKnob(_Wire):
    name: str
    label: str
    unit: str
    searched: bool
    low: float = Field(description="The searched range's low end, or the held value.")
    high: float
    step: float | None
    values: int | None = Field(description="Settings the knob can take: 1 when held; null when its range is not valid.")
    importance: int | None
    current: float = Field(description="The frozen incumbent's value.")
    domain_low: float
    domain_high: float
    low_position: float | None = Field(description="Positions in the legal domain, 0 to 1; null for a domain of one value.")
    high_position: float | None
    current_position: float | None
    start: float | None = Field(description="Where Zoom starts (the seed's value), which it keeps in every round; null for Grid.")
    start_position: float | None


class GoldenSearchStageWorkload(_Wire):
    stage: str
    label: str
    planned: int = Field(description="The receipt's planned maximum evaluations: an upper bound, never a stopping point.")
    used: int = Field(description="Engine runs used so far, runs in flight included; the stages' together equal the study's consumed evaluations.")


class GoldenSearchWorkload(_Wire):
    cap: int
    consumed: int
    planned_total: int
    stages: list[GoldenSearchStageWorkload]


class GoldenSearchMinimumYear(_Wire):
    year: int
    selected_sessions: int
    year_sessions: int


class GoldenSearchMinimumWindow(_Wire):
    key: str
    label: str
    minimum_trades: int | None
    trading_years: float | None = Field(description="Σ selected / scheduled sessions per year; null on a fixed-floor plan.")
    years: list[GoldenSearchMinimumYear]


class GoldenSearchMinimums(_Wire):
    expected_trades_per_year: int | None = Field(description="Null on a fixed-floor plan.")
    windows: list[GoldenSearchMinimumWindow]


class GoldenSearchCoverageMonth(_Wire):
    month_start_ms: InstantMs
    year: int
    month: int = Field(ge=1, le=12)
    sessions: int
    complete: int
    fetching: int
    stale: int
    failed: int
    missing: int


class GoldenSearchCoverageMeasured(_Wire):
    status: Literal["measured"]
    months: list[GoldenSearchCoverageMonth]


class GoldenSearchCoverageMissing(_Wire):
    status: Literal["missing"]
    reason: str


GoldenSearchCoverage = Annotated[GoldenSearchCoverageMeasured | GoldenSearchCoverageMissing, Field(discriminator="status")]


class GoldenSearchPlanCharts(_Wire):
    windows: list[GoldenSearchPlanWindow]
    search_space: list[GoldenSearchPlanKnob] = Field(description="Every declared knob, in the plan's search order.")
    workload: GoldenSearchWorkload
    minimums: GoldenSearchMinimums
    coverage: GoldenSearchCoverage


class GoldenSearchScoredNumbers(_Wire):
    """A scored point's development numbers under the window's frozen selection policy."""

    sharpe_ratio: float | None
    net_profit: float | None
    total_return_pct: float | None
    total_trades: int | None
    max_drawdown_pct: float | None
    ineligibility: str | None = Field(description="Why the rules reject the point; null when it is eligible.")
    objective: float | None = Field(description="The policy's objective; null when undefined.")


class GoldenSearchTriedPoint(GoldenSearchScoredNumbers):
    order: int = Field(description="0 for the seed, then each evaluated value in the order the procedure tried it.")
    pass_index: int | None = Field(description="Null for the seed.")
    knob: str | None
    round_index: int | None
    value: float | None = Field(description="The knob's value this point tried; null for the seed.")
    point: dict[str, Any]
    best_so_far: float | None = Field(description="The highest objective among the eligible points tried so far; null before one is eligible.")


class GoldenSearchConvergenceMeasured(_Wire):
    status: Literal["measured"]
    tried: list[GoldenSearchTriedPoint]


class GoldenSearchConvergenceMissing(_Wire):
    status: Literal["missing"]
    reason: str


GoldenSearchConvergence = Annotated[GoldenSearchConvergenceMeasured | GoldenSearchConvergenceMissing, Field(discriminator="status")]


class GoldenSearchKnobMove(_Wire):
    name: str
    label: str
    unit: str
    low: float
    high: float
    start: float = Field(description="The seed's value.")
    retained: float = Field(description="The winner's value.")
    start_position: float | None = Field(description="(start − low) / (high − low); null for a range of one value.")
    retained_position: float | None
    moved: bool
    edge_hit: bool = Field(description="The retained value is an end of the searched range.")


class GoldenSearchHeldKnob(_Wire):
    name: str
    label: str
    value: float


class GoldenSearchProfilePoint(GoldenSearchScoredNumbers):
    value: float
    retained: bool = Field(description="The winner's value of this knob.")


class GoldenSearchKnobProfile(_Wire):
    name: str
    label: str
    unit: str
    low: float
    high: float
    pass_index: int | None = Field(description="Zoom: the last pass that searched this knob; null for Grid, whose profile slices the scored points through the winner.")
    held: list[GoldenSearchHeldKnob] = Field(description="Every other knob's value while this one varied.")
    points: list[GoldenSearchProfilePoint] = Field(description="By value, ascending.")


class GoldenSearchScoredPoint(GoldenSearchScoredNumbers):
    point: dict[str, Any]
    point_hash: str
    winner: bool


class GoldenSearchChartPolicy(_Wire):
    objective: RankingMeasure
    min_trades: int | None = Field(description="This window's trade floor.")
    max_drawdown_ceiling: float
    require_positive_net: bool


class GoldenSearchProcedureCharts(_Wire):
    key: Literal["search", "recent"]
    method: Literal["zoom", "grid"]
    window: GoldenSearchWindow
    policy: GoldenSearchChartPolicy
    winner_hash: str
    convergence: GoldenSearchConvergence
    moves: list[GoldenSearchKnobMove] = Field(description="Each searched knob, in the plan's order.")
    profiles: list[GoldenSearchKnobProfile] = Field(description="Each searched knob, in the plan's order; empty when the Zoom path cannot be rebuilt.")
    points: list[GoldenSearchScoredPoint] = Field(description="Every scored base-scenario point on the window, in the order first asked for.")


class GoldenSearchSearchCharts(_Wire):
    procedures: list[GoldenSearchProcedureCharts] = Field(description="The all-period search, then the recent fit, as far as the study has recorded them.")


class GoldenSearchFoldChart(_Wire):
    fold_index: int
    train_start_ms: InstantMs
    train_end_ms: InstantMs
    test_start_ms: InstantMs
    test_end_ms: InstantMs
    winner: dict[str, Any] | None = Field(description="The settings the fold's training chose; null without a winner.")
    status: Literal["planned", "pending", "completed", "failed"] = Field(description="planned: from the receipt, before testing over time ran.")
    failure_reason: str | None
    train_sharpe: float | None = Field(description="The fold winner's training Sharpe; null without a winner.")
    test_sharpe: float | None = Field(description="Its test Sharpe; null unless the fold completed.")
    retention: float | None = Field(description="Test Sharpe over training Sharpe as the verdict reads it; null unless the fold completed with a positive training Sharpe.")
    test_return: float | None = Field(description="The winner's test return, a fraction of the fold's fresh capital.")
    incumbent_return: float | None = Field(description="The frozen incumbent's return on the same test window.")
    return_difference: float | None = Field(description="test_return less incumbent_return; null unless both exist.")
    test_trades: int | None
    incumbent_trades: int | None
    incumbent_failure: str | None = Field(description="Why the incumbent's run on this test window failed; null when it completed or never ran.")


class GoldenSearchKnobDrift(_Wire):
    name: str
    label: str
    unit: str
    low: float = Field(description="The searched range's low end.")
    high: float
    current: float = Field(description="The frozen incumbent's value.")
    all_period: float | None = Field(description="The all-period winner's value; null before the search finished.")
    folds: list[float | None] = Field(description="Each fold winner's value, in fold order; null for a fold without a winner.")


class GoldenSearchTestOverTimeCharts(_Wire):
    planned: bool = Field(description="Testing over time has not run: the folds are the receipt's plan.")
    in_progress: bool = Field(description="Testing over time is running or stopped short of its verdict: totals count only the folds completed so far.")
    folds: list[GoldenSearchFoldChart]
    linked: list[GoldenSearchLinkedReturn]
    incumbent_linked: list[GoldenSearchLinkedReturn]
    retention_threshold: float
    median_retention: float | None
    test_trades_total: int | None = Field(description="The verdict's test trades; while in progress, the completed folds' so far; null before testing over time ran.")
    forward_minimum: int | None = Field(description="The trades all forward tests together must reach, as the verdict judged them.")
    below_minimum: bool | None = Field(description="The finished verdict's total is under the forward minimum; null while in progress or when either is missing.")
    drift: list[GoldenSearchKnobDrift] = Field(description="Each searched knob, in the plan's order.")


class GoldenSearchSummaryPills(_Wire):
    judged: str
    test_trades: int | None = Field(description="The verdict's test trades; null before it is computed.")
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


# ── Chance readings (#3011, ADR 0074 decision 12) ────────────────────────


class GoldenSearchChanceWindow(_Wire):
    start_ms: InstantMs
    end_ms: InstantMs


class GoldenSearchChanceObjective(_Wire):
    measure: RankingMeasure
    label: str = Field(description="How the objective reads in a sentence.")
    units: str = Field(description="What one unit of the objective counts.")


class GoldenSearchChanceSeed(_Wire):
    inputs: list[str | int] = Field(description="The exact parts the seed was derived from; empty when nothing was drawn.")
    value: int = Field(description="The derived seed itself, so a stored reading is reproducible without rederiving it.")


class GoldenSearchChanceSource(_Wire):
    """The runs a reading actually resampled: their shared window, their point hashes, their series digests."""

    window: GoldenSearchChanceWindow
    session_count: int
    candidate_point_hash: str = ""
    incumbent_point_hash: str = Field(default="", description="Empty when the reading is the candidate's alone.")
    series_digest: dict[str, str] = Field(default_factory=dict, description="Digests of the series actually resampled, keyed by the side they belong to.")
    capital: float | None = None


class GoldenSearchChanceBasis(_Wire):
    """Everything a stored reading was drawn and judged under, so reading it back never reclassifies with today's constants."""

    schema_version: str = Field(description="The reading's schema/interpretation version.")
    convention: str
    replications: int
    chunk_replications: int
    mean_block_sessions: float
    thresholds: dict[str, float] = Field(description="The interpretation thresholds the stored labels were judged by.")
    objective: GoldenSearchChanceObjective
    seed: GoldenSearchChanceSeed = Field(description="The exact inputs the seed was derived from and the seed itself.")
    source: GoldenSearchChanceSource


class GoldenSearchChanceCounts(_Wire):
    better: int
    tied: int
    worse: int
    undefined: int


class GoldenSearchChanceRange(_Wire):
    p5: float | None = Field(description="5th percentile by linear interpolation over the finite values; null when there are none.")
    p50: float | None
    p95: float | None
    finite: int = Field(description="How many resamples produced a finite value.")


class GoldenSearchChanceAtLeastAsGood(_Wire):
    count: int = Field(description="better + tied, the final test's inclusive rule.")
    share: float = Field(description="Over every replication, not only the defined pairs.")


class GoldenSearchChanceComparisonMeasured(_Wire):
    """The paired comparison of one fit's development result against the current settings', under the resampling model only."""

    status: Literal["measured"]
    label: Literal["clearly_as_good", "clearly_worse", "identical_outcomes", "mixed", "no_defined_paths"]
    sentence: str = Field(description="The decision sentence; Python writes it, the browser only displays it.")
    incomplete_support: bool = Field(description="True when undefined paths leave the support incomplete.")
    bias_note: str = Field(description="The selection-bias caveat displayed beside the chart.")
    counts: GoldenSearchChanceCounts = Field(description="Raw event counts over every replication; they sum to the replication count.")
    shares: dict[str, float] = Field(description="Presentation shares; labels come from the counts, never from these rounded values.")
    at_least_as_good: GoldenSearchChanceAtLeastAsGood
    candidate_range: GoldenSearchChanceRange
    difference_range: GoldenSearchChanceRange = Field(description="Candidate minus incumbent in the objective's units.")
    basis: GoldenSearchChanceBasis


class GoldenSearchChanceComparisonSameSettings(_Wire):
    status: Literal["same_settings"]
    sentence: str
    basis: GoldenSearchChanceBasis


class GoldenSearchChanceComparisonBaseline(_Wire):
    status: Literal["baseline"]
    sentence: str
    basis: GoldenSearchChanceBasis


class GoldenSearchChanceReadingMissing(_Wire):
    status: Literal["missing"]
    code: str = Field(description="A stable identifier for the operator copy in `reason`.")
    reason: str


class GoldenSearchChanceReadingFailed(_Wire):
    status: Literal["failed"]
    reason: str = Field(description="The diagnostic: an unexpected computation error, logged with context when it happened.")
    sentence: str = Field(description="The operator sentence for the failure; Python writes it, the browser only displays it.")


GoldenSearchChanceComparison = Annotated[
    GoldenSearchChanceComparisonMeasured
    | GoldenSearchChanceComparisonSameSettings
    | GoldenSearchChanceComparisonBaseline
    | GoldenSearchChanceReadingMissing
    | GoldenSearchChanceReadingFailed,
    Field(discriminator="status"),
]


class GoldenSearchChanceNetReturnQuantity(_Wire):
    """What the profitability range's numbers count — compounded net return, never the plan objective (#3026)."""

    measure: Literal["compounded_net_return"]
    label: str = Field(description="How the quantity reads in a sentence.")
    units: str = Field(description="What one unit counts; a fraction of the starting capital.")


class GoldenSearchChanceNetReturnRange(_Wire):
    """The 5th/50th/95th percentiles of the resampled compounded net return, beside the counts it summarizes (#3026)."""

    quantity: GoldenSearchChanceNetReturnQuantity
    p5: float | None = Field(description="Null when no resample produced a defined compounded return.")
    p50: float | None
    p95: float | None
    finite: int = Field(description="How many resamples produced a finite compounded return.")


class GoldenSearchChanceProfitabilityMeasured(_Wire):
    """The fit's own development outcome resampled: how often the compounded net return was positive, flat or negative."""

    status: Literal["measured"]
    label: Literal["clearly_positive", "clearly_negative", "mixed", "no_defined_paths"]
    sentence: str
    counts: dict[str, int] = Field(description="positive, zero, negative and undefined counts over every replication.")
    shares: dict[str, float]
    net_return_range: GoldenSearchChanceNetReturnRange | None = Field(
        default=None,
        description="The compounded-net-return percentiles recorded beside the counts (#3026); null when the stored reading predates them — never backfilled.",
    )
    basis: GoldenSearchChanceBasis


GoldenSearchChanceProfitability = Annotated[
    GoldenSearchChanceProfitabilityMeasured | GoldenSearchChanceReadingMissing | GoldenSearchChanceReadingFailed,
    Field(discriminator="status"),
]


class GoldenSearchChanceRung(_Wire):
    """One rung of the rehearsal ladder: what share of its resamples met each modeled rule."""

    k: int = Field(description="The multiple of the frozen final interval's scheduled sessions.")
    sessions: int = Field(description="Scheduled sessions this rung rehearses over; ordinal sessions, never invented dates.")
    trade_floor: int
    denominator: int = Field(description="Every resampled path, the denominator of each share.")
    passes: dict[str, int] = Field(description="Pass counts per rule and for the joint predicate, keyed `pass_<rule>` and `pass_joint`.")
    undefined: dict[str, int] = Field(default_factory=dict, description="Undefined counts for the rules that can be, keyed `undefined_<rule>`; the exit rule never is.")
    shares: dict[str, float] = Field(description="Presentation shares; labels come from the counts, never from these rounded values.")


class GoldenSearchChanceRehearsalSeeds(_Wire):
    """Each rung's exact seed, by its multiple: the 1× seed is what the final-test slice reproduces the carry-over from."""

    inputs: list[str | int]
    value: int


class GoldenSearchChanceRehearsalRules(_Wire):
    """The rules a rehearsal modeled, as the plan froze them."""

    max_drawdown_ceiling: float = Field(description="A fraction of peak equity.")
    require_positive_net: bool
    drawdown_basis: str = Field(description="Always 'session closes'; the real final-test gate reads the engine's per-bar drawdown.")


class GoldenSearchChanceRehearsalActivity(_Wire):
    """The trade-floor basis the rungs' floors came from."""

    frequency_based: bool
    expected_trades_per_year: int | None
    final_trading_years: float | None = Field(description="The frozen final interval's length in trading years; null on a fixed-floor plan.")


class GoldenSearchChanceWindowSessions(_Wire):
    """A window and its scheduled session count: a rehearsal's frozen final interval, or a drawdown's source window."""

    window: GoldenSearchChanceWindow
    scheduled_sessions: int


class GoldenSearchChanceRehearsalFinalInterval(GoldenSearchChanceWindowSessions):
    """The frozen final interval whose scheduled session count the rungs scale from."""


class GoldenSearchChanceRehearsalPercentiles(_Wire):
    """The 1× objective's 1st–99th percentiles by linear interpolation over the finite values, for the later carry-over display."""

    p: list[float] = Field(description="Empty when no resample produced a defined objective.")
    finite: int


class GoldenSearchChanceRehearsalBasis(_Wire):
    """Everything a stored rehearsal was drawn and judged under, so reading it back never reclassifies it."""

    schema_version: str
    convention: str
    replications: int
    chunk_replications: int
    mean_block_sessions: float
    thresholds: dict[str, float] = Field(description="The interpretation thresholds the stored labels were judged by.")
    objective: GoldenSearchChanceObjective
    rules: GoldenSearchChanceRehearsalRules
    activity: GoldenSearchChanceRehearsalActivity
    final_interval: GoldenSearchChanceRehearsalFinalInterval
    source: GoldenSearchChanceSource
    seeds: dict[str, GoldenSearchChanceRehearsalSeeds]


class GoldenSearchChanceRehearsalMeasured(_Wire):
    """The final-test rehearsal ladder: conditional resampling support for each modeled rule, never a power calculation."""

    status: Literal["measured"]
    rungs: list[GoldenSearchChanceRung]
    active_rules: list[str] = Field(description="The rules this plan models; a rule the plan does not require has no series here.")
    guidance: str = Field(description="The owner-facing diagnosis; Python writes it, the browser only displays it.")
    objective_percentiles_1x: GoldenSearchChanceRehearsalPercentiles
    basis: GoldenSearchChanceRehearsalBasis


GoldenSearchChanceRehearsal = Annotated[
    GoldenSearchChanceRehearsalMeasured | GoldenSearchChanceReadingMissing | GoldenSearchChanceReadingFailed,
    Field(discriminator="status"),
]


class GoldenSearchChanceDrawdownPercentiles(_Wire):
    """The resampled maximum fall's percentiles by linear interpolation over the finite paths."""

    p50: float | None = Field(description="Null when no resample produced a defined fall.")
    p95: float | None
    finite: int


class GoldenSearchChanceDrawdownBasis(_Wire):
    """Everything a stored drawdown reading was drawn and judged under, so reading it back never reclassifies it."""

    schema_version: str
    convention: str
    replications: int
    chunk_replications: int
    mean_block_sessions: float
    thresholds: dict[str, float] = Field(description="The frozen ceiling the stored labels were judged by.")
    horizon: GoldenSearchChanceWindowSessions = Field(description="The source window whose own scheduled session count is the horizon.")
    seed: GoldenSearchChanceSeed
    source: GoldenSearchChanceDrawdownSource


class GoldenSearchChanceDrawdownSource(_Wire):
    """The run a drawdown reading actually resampled."""

    point_hash: str
    series_digest: str = Field(description="Digest of the series actually resampled.")
    capital: float
    recorded_engine_per_bar: float | None = Field(description="The engine figure carried verbatim; null when the run kept none.")


class GoldenSearchChanceDrawdownMeasured(_Wire):
    """The resampled session-close drawdown range beside the recorded falls, without sizing advice."""

    status: Literal["measured"]
    recorded_close_only: float = Field(description="The recorded development run's own worst fall at session closes, starting capital in the initial peak.")
    recorded_engine_per_bar: float | None = Field(description="The engine's bar-by-bar worst fall as recorded; null is a named missing marker, never zero.")
    percentiles: GoldenSearchChanceDrawdownPercentiles
    counts: dict[str, int] = Field(description="Finite falls strictly beyond the ceiling, at or within it, and undefined — all over every replication.")
    shares: dict[str, float] = Field(description="Presentation shares; labels come from the counts, never from these rounded values.")
    sentence: str = Field(description="The owner-facing reading; Python writes it, the browser only displays it.")
    concern: bool = Field(description="True when the 95th percentile is strictly beyond the stored ceiling; equality is within it.")
    basis: GoldenSearchChanceDrawdownBasis


GoldenSearchChanceDrawdown = Annotated[
    GoldenSearchChanceDrawdownMeasured | GoldenSearchChanceReadingMissing | GoldenSearchChanceReadingFailed,
    Field(discriminator="status"),
]


class GoldenSearchChance(_Wire):
    """One fit's development chance components, each normalized independently of the others."""

    comparison: GoldenSearchChanceComparison
    profitability: GoldenSearchChanceProfitability
    rehearsal: GoldenSearchChanceRehearsal
    drawdown: GoldenSearchChanceDrawdown


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
    chance: GoldenSearchChance
    edge_hits: list[str]
    guidance: GoldenSearchCandidateGuidance
    flags: list[GoldenSearchFinding]
    params_sentence: str
    fixed_sentence: str


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


class GoldenSearchFitToTest(_Wire):
    """One set of fitted settings the final test runs, under the candidates that chose it."""

    key: CandidateKey
    same_as: list[CandidateKey]
    label: str


class GoldenSearchEvidenceView(_Wire):
    candidates: list[GoldenSearchEvidenceCandidate]
    fits_to_test: list[GoldenSearchFitToTest] = Field(
        description="What opening the final test will run beside the current settings; empty when every fit returned them."
    )
    pair_maps: list[GoldenSearchPairMap]
    recommendation: GoldenSearchRecommendation
    scope: GoldenSearchEvidenceScope
    incomplete: bool


class GoldenSearchExamCheck(_Wire):
    code: str
    label: str
    status: CheckStatus
    detail: str


class GoldenSearchChanceCarryOverBasis(_Wire):
    """The reproduced rehearsal's identity: its schema, its seed, its scheduled length and its source digest."""

    schema_version: str
    rehearsal_schema: str | None = Field(description="Null when the stored rehearsal predates schema versions.")
    seed: GoldenSearchChanceSeed
    scheduled_sessions: int
    objective: GoldenSearchChanceObjective
    source_digest: str | None


class GoldenSearchChanceCarryOverMeasured(_Wire):
    """The recorded final result's rank among its own reproduced 1× development rehearsal: a display diagnostic, never an approval test."""

    status: Literal["measured"]
    label: Literal["below_most", "above_most", "middle", "no_defined_paths"]
    rank: float = Field(description="Strictly-below count over the finite count; 1.0 means above every finite rehearsal.")
    below: int
    finite: int
    undefined: int
    recorded_objective: float
    curve: GoldenSearchChanceRehearsalPercentiles = Field(description="The reproduced rehearsal's 1st-99th percentiles, plot summaries and never individual rehearsals.")
    sentence: str
    selection_note: str = Field(description="The unadjusted-for-selection caveat carried beside the rank.")
    basis: GoldenSearchChanceCarryOverBasis


GoldenSearchChanceCarryOver = Annotated[
    GoldenSearchChanceCarryOverMeasured | GoldenSearchChanceReadingMissing | GoldenSearchChanceReadingFailed,
    Field(discriminator="status"),
]


class GoldenSearchFinalChance(_Wire):
    """One tested fit's final chance components, each normalized independently of the others."""

    comparison: GoldenSearchChanceComparison
    profitability: GoldenSearchChanceProfitability
    carry_over: GoldenSearchChanceCarryOver


class GoldenSearchChanceFitToFitMeasured(_Wire):
    """Two distinct fits' final comparison from one shared paired draw, both directions summarized from it."""

    status: Literal["measured"]
    pair: list[str] = Field(description="The two point hashes, canonically ordered, that were compared.")
    counts: GoldenSearchChanceCounts
    shares: dict[str, float]
    at_least_as_good: GoldenSearchChanceAtLeastAsGood
    sentence: str
    selection_note: str
    basis: GoldenSearchChanceFitToFitBasis


class GoldenSearchChanceFitToFitSource(_Wire):
    """The two final runs a fit-to-fit reading actually resampled."""

    window: GoldenSearchChanceWindow
    session_count: int
    point_hashes: list[str] = Field(description="The two hashes, canonically ordered.")
    series_digests: list[str]
    capital: float


class GoldenSearchChanceFitToFitBasis(_Wire):
    """Everything a stored fit-to-fit reading was drawn under, so reading it back never reclassifies it."""

    schema_version: str
    convention: str
    replications: int
    mean_block_sessions: float
    thresholds: dict[str, float]
    objective: GoldenSearchChanceObjective
    seed: GoldenSearchChanceSeed
    source: GoldenSearchChanceFitToFitSource


GoldenSearchChanceFitToFit = Annotated[
    GoldenSearchChanceFitToFitMeasured | GoldenSearchChanceReadingMissing | GoldenSearchChanceReadingFailed,
    Field(discriminator="status"),
]


class GoldenSearchExamChance(_Wire):
    """The exam-level chance record: the optional fit-to-fit comparison, absent by design when one fit ran."""

    fit_to_fit: GoldenSearchChanceFitToFit


class GoldenSearchExamFit(_Wire):
    """One fit that sat the final test, judged on its own under the frozen rules."""

    key: CandidateKey
    same_as: list[CandidateKey] = Field(description="Other fits that chose exactly these settings and so share this result.")
    label: str
    point: dict[str, Any]
    outcome: ExamOutcome | None
    checks: list[GoldenSearchExamCheck]
    retention: float | None = Field(description="Descriptive only, never a check.")
    metrics: GoldenSearchMetrics | None
    chance: GoldenSearchFinalChance
    weakness: list[GoldenSearchFinding] = Field(
        description="What approving this fit needs the owner to acknowledge as weak; empty when its evidence meets the rules."
    )


class GoldenSearchExamView(_Wire):
    window: GoldenSearchWindow
    claim: Claim
    exposure_state: ExposureState
    chance: GoldenSearchExamChance
    fits: list[GoldenSearchExamFit] = Field(description="Every fit that ran on the final interval; one for a final test recorded with a locked candidate.")
    incumbent_metrics: GoldenSearchMetrics | None
    pick: Literal["before_test", "after_test"] = Field(description="Whether the owner named the fit before the test opened or after its outcomes.")
    chosen_key: CandidateKey | None = Field(description="The fit the owner named; null until an approval names one.")
    selection_note: str | None = Field(description="What the record says about a pick made with two final-test results in view; null otherwise.")
    positive_net_required: bool = Field(
        description="The frozen plan's positive-net rule, read from the study (#3026): it decides whether the money-support row and its approval test exist."
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
        description="What opening the final test would record, while the candidates are compared; null otherwise."
    )


# Reads beside the study.


class GoldenSearchRunGroup(_Wire):
    key: RunGroup
    label: str
    runs: int
    completed: int = Field(description="How many of the group's runs finished with a result.")
    with_curve: int = Field(description="How many kept their daily equity.")


class GoldenSearchRun(_Wire):
    """One engine run of a study, under the step that first asked for it (#2930)."""

    evaluation_key: str
    label: str = Field(description="What asked for the run, its cost scenario when stressed, and the names the study gives its settings.")
    params_sentence: str = Field(description="The run's settings in words; with the label it tells any two runs of a study apart.")
    window_start_ms: InstantMs
    window_end_ms: InstantMs
    status: Literal["pending", "completed", "failed"]
    total_trades: int | None
    total_return_pct: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float | None
    error: str | None
    has_curve: bool


class GoldenSearchRunsPage(_Wire):
    total: int = Field(description="Runs matching the group and fold asked for.")
    page: int
    page_size: int
    sort: RunSort = Field(description="The order of the rows: as recorded, or by a measure.")
    folds: int = Field(description="How many walk-forward folds the plan froze; fold runs can be listed one fold at a time.")
    groups: list[GoldenSearchRunGroup] = Field(description="Every group the study has runs in, whatever the filter.")
    rows: list[GoldenSearchRun]


class GoldenSearchRunCurve(SessionCurve):
    evaluation_key: str


class GoldenSearchRunCurves(_Wire):
    sessions_ms: SessionsMs = Field(description="Every session close any returned curve scored, in order, sent once.")
    curves: list[GoldenSearchRunCurve]
    not_recorded: list[str] = Field(description="The runs asked for that kept no daily equity; never drawn as a flat line.")


class GoldenSearchCurveLine(_Wire):
    """One run a step draws: its role says how, its label what to call it on that chart."""

    role: LineRole
    label: str
    current_settings: bool = Field(description="The run's settings are the current settings, whatever its role: a winner can be the incumbent.")
    params_sentence: str = Field(description="The run's settings in words.")
    curve: GoldenSearchRunCurve


class GoldenSearchCurveSet(_Wire):
    sessions_ms: SessionsMs = Field(description="Every session close any line of the set scored, in order, sent once.")
    lines: list[GoldenSearchCurveLine]
    left_out: int = Field(description="Other scored points with a curve that this read did not return, past its cap; named lines are never left out.")
    not_recorded: int = Field(description="Completed runs of the set that kept no daily equity.")
    failed: int = Field(description="Runs of the set that failed, so they have no curve.")
    notes: list[str] = Field(description="What the set could not show, in words, e.g. that the current settings were not run on this window.")


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
    bars_held: int | None = Field(description="Decision bars that closed after the entry and by the exit, on the trading calendar; null when hold times are not counted.")
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
    rule: Literal["freedman_diaconis", "sturges", "capped", "single"] = Field(
        description="What set the width: Freedman–Diaconis; Sturges when the middle half of the trades net the same; the bin cap; one bin when every trade does."
    )
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
    bar_span_ms: int | None = Field(description="The strategy's decision bar, in ms (a day or longer means one bar per session); null when hold times are not counted.")
    hold_reason: str | None = Field(description="Why hold times are not counted; null when they are.")
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


class GoldenSearchFinalMeasure(_Wire):
    key: Literal["annualized_return", "sharpe_ratio", "max_drawdown_pct", "trades_per_year"]
    label: str
    development: float | None = Field(description="Over the development period; null when its run failed or did not record it.")
    final: float | None = Field(description="Over the final test.")
    change: float | None = Field(description="final − development; null unless both exist.")


class GoldenSearchRunDetail(_Wire):
    window: GoldenSearchWindow
    metrics: GoldenSearchMetrics
    cumulative_return: list[GoldenSearchCumulativeReturnPoint]
    daily_equity: list[GoldenSearchEquityPoint]
    drawdown: list[GoldenSearchDrawdownPoint]
    monthly: list[GoldenSearchMonthlyResult]
    concentration_curve: GoldenSearchConcentrationCurve | None = Field(description="The development run's; null on a final-test run.")
    trade_charts: GoldenSearchTradeCharts | None = Field(description="The development run's; null on a final-test run.")
    comparison: list[GoldenSearchFinalMeasure] | None = Field(description="A final-test run's measures beside development's; null on a development run.")


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
