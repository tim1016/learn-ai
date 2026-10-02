/**
 * Wire shapes for Golden Search (#2696). Each response shape is derived from
 * the generated contract (`api/broker.types.ts`, from
 * PythonDataService/app/schemas/golden_search.py), so a field the server adds,
 * drops or retypes is a compile error here rather than a silent drift. The
 * contract types a parameter point as an empty object, so every field that
 * carries one is re-typed as `Point`; the only other overrides keep pair
 * tuples readonly and key action refusals by command. The plan sent to the
 * server reuses the frozen protocol echo's snake_case shape, which the
 * server accepts beside its camelCase request aliases; command envelopes
 * stay hand-written. Every temporal value is int64 ms UTC; interval
 * boundaries are ET-midnight instants with exclusive ends. Every number
 * shown is computed in Python — these types carry it, the browser never
 * derives it.
 */

import type { components } from '../../api/broker.types';

export type { RankingMeasure } from '../grid-search/grid-search.types';
export type { FoldPlan } from '../walk-forward-study/walk-forward-study.types';

type Schemas = components['schemas'];

/** One canonical parameter value: a point is a dict of JSON scalars (integers as ints, decimals quantized). */
export type PointValue = string | number | boolean | null;
/** A complete canonical parameter assignment, `symbol` included. */
export type Point = Readonly<Record<string, PointValue>>;

// ---------------------------------------------------------------- capabilities

export type CapabilityKnob = Schemas['GoldenSearchCapabilityKnob'];
export type KnobKind = CapabilityKnob['kind'];
export type FixedControl = Schemas['GoldenSearchFixedControl'];
export type KnobConstraint = Schemas['GoldenSearchConstraint'];
export type KnobPair = readonly [string, string];
export type StrategyCapability = Schemas['GoldenSearchCapability'];

// ---------------------------------------------------------------- protocol

/**
 * One declared knob: searched over `[low, high]` at its smallest `step` (a
 * multiple of the declared quantum; Grid samples at it, Zoom stops refining
 * below it), or held at `fixed_value`.
 */
export type KnobPlan = Schemas['GoldenSearchKnobPlan'];
export type KnobMode = KnobPlan['mode'];
export type GoldenSearchMethod = Schemas['GoldenSearchProtocol']['method'];
/** `max_drawdown_ceiling` is a fraction of peak equity, in (0, 1]. */
export type SelectionPolicy = Schemas['GoldenSearchSelectionPolicy'];
export type ZoomSettings = Schemas['GoldenSearchZoomSettings'];
export type ExecutionAssumptions = Schemas['GoldenSearchExecution'];
export type StressScenario = Schemas['GoldenSearchStressScenario'];

export type IncumbentRef = Omit<Schemas['GoldenSearchIncumbent'], 'params'> & { params: Point };

/**
 * The plan, field for field the frozen `GoldenSearchProtocol` the server
 * echoes; `knobs` lists every declared knob in search order, and `seed`
 * omitted or null starts from the incumbent's params.
 */
export type ProtocolRequest = Omit<Schemas['GoldenSearchProtocol'], 'seed' | 'incumbent' | 'pair_audits'> & {
  seed?: Point | null;
  incumbent: IncumbentRef;
  pair_audits: KnobPair[];
};

export type ProtocolRefusal = Schemas['GoldenSearchProtocolRefusal'];
export type TradeActivity = Schemas['GoldenSearchActivity'];

// ---------------------------------------------------------------- exposure, defaults, preflight

export type ExposureView = Schemas['GoldenSearchExposure'];
export type ExposureState = ExposureView['state'];
export type ExposureClaim = NonNullable<Schemas['GoldenSearchStudySummary']['exposure_claim']>;

/**
 * `GET /defaults`: a full plan prefilled, plus the incumbent's name, the
 * proposed final interval's exposure and the final test's length in months.
 * Those three are not plan fields: the server refuses them in a plan.
 */
export type GoldenSearchDefaults = Omit<Schemas['GoldenSearchDefaults'], 'seed' | 'incumbent' | 'pair_audits'> & {
  seed: Point;
  incumbent: IncumbentRef;
  pair_audits: KnobPair[];
};

/**
 * The month counts `GET /defaults` lays the intervals out from: the final
 * test's length and the fold lengths. The server computes the dates (the
 * calendar authority stays in Python); the Plan form re-asks when one changes.
 */
export interface DefaultsMonths {
  final_months: number;
  training_months: number;
  test_months: number;
}

export type StageEstimate = Schemas['GoldenSearchStageEstimate'];
export type StudyEstimate = Schemas['GoldenSearchEstimate'];
export type RunUpView = Schemas['GoldenSearchRunUp'];
/** `POST /preflight`: protocol problems are refusals in the body, never a 400. */
export type GoldenSearchPreflight = Schemas['GoldenSearchPreflight'];

// ---------------------------------------------------------------- study lifecycle

export type StudySummary = Schemas['GoldenSearchStudySummary'];
export type StudyState = StudySummary['state'];
/** The fence status of the current stage run as the lifecycle presents it (`running` with no live job reads `interrupted`). */
export type PresentedStatus = StudySummary['presented_status'];
export type StudyCommandName = Schemas['GoldenSearchCommandRequest']['command'];
export type CandidateKey = Schemas['GoldenSearchEvidenceCandidate']['key'];
export type ExamOutcome = NonNullable<StudySummary['exam_outcome']>;
export type RetainKind = 'keep_current' | 'wait_for_fresh_data' | 'retain_exploration';

export type StudyReceiptSummary = Schemas['GoldenSearchReceiptSummary'];
export type StudyGuidance = Schemas['GoldenSearchGuidance'];
export type StudyProgress = Schemas['GoldenSearchProgress'];
export type StageDispatch = Schemas['GoldenSearchDispatch'];
export type StudyDecision = Schemas['GoldenSearchDecision'];
/** Returns and drawdowns are fractions, as the engine reports them. */
export type Metrics = Schemas['GoldenSearchMetrics'];

/** One refinement round of one knob; `invalid`, `results` and `objectives` arrive as two-element arrays. */
export type ZoomRound = Schemas['GoldenSearchZoomRound'];
/** One searched knob's outcome over the whole procedure, authored in Python from its rounds. */
export type KnobSummary = Schemas['GoldenSearchKnobSummary'];
export type StopReason = KnobSummary['stop_reason'];
export type ProcedureCounts = Schemas['GoldenSearchProcedureCounts'];

/** The recent-window procedure. */
export type ProcedureView = Omit<Schemas['GoldenSearchProcedureView'], 'winner'> & { winner: Point };
/** The all-period procedure, with its pair landscapes centered on its winner; `pair_maps_incomplete` when the budget cut them short. */
export type SearchView = Omit<Schemas['GoldenSearchSearchView'], 'winner'> & { winner: Point };

/** One fold; `incumbent_test_metrics` is the frozen incumbent on the same test window, the benchmark every fold is read against. */
export type ValidationFold = Omit<Schemas['GoldenSearchValidationFold'], 'winner'> & { winner: Point | null };
export type FoldRunStatus = ValidationFold['status'];
/** `compute_verdict(...).as_dict()`: the legacy five-label summary with its coverage. */
export type ValidationVerdict = Schemas['GoldenSearchVerdict'];
/** Growth of 1 linked across test folds, minus 1; `linked_return` is null once a fold is missing (the line breaks). */
export type LinkedFoldReturn = Schemas['GoldenSearchLinkedReturn'];
export type ValidationSummaryPills = Schemas['GoldenSearchSummaryPills'];
/** `incumbent_linked` is the frozen incumbent's test returns linked the same way; `incomplete` when the budget stopped the stage. */
export type ValidationView = Omit<Schemas['GoldenSearchValidationView'], 'folds'> & { folds: ValidationFold[] };

/** `center` marks the candidate's own value in a neighborhood. */
export type NeighborRow = Schemas['GoldenSearchNeighborRow'];
export type EvidenceCellStatus = NeighborRow['status'];
export type CandidateNeighborhood = Schemas['GoldenSearchNeighborhood'];
export type StressResult = Schemas['GoldenSearchStressResult'];
export type Finding = Schemas['GoldenSearchFinding'];
export type Recommendation = Schemas['GoldenSearchRecommendation'];
export type PairMapCell = Schemas['GoldenSearchPairCell'];
/** A two-knob landscape: rows are `y_knob`'s values, columns `x_knob`'s, cells row-major. */
export type PairMap = Schemas['GoldenSearchPairMap'];
export type IntervalMs = Schemas['GoldenSearchWindow'];
export type EvidenceScope = Schemas['GoldenSearchEvidenceScope'];

/**
 * One candidate on the development scope. `guidance` comes from a closed copy
 * map keyed by its situation; `params_sentence` is Python-authored; the
 * incumbent is not `exam_eligible` (keeping it needs no final test).
 */
export type EvidenceCandidate = Omit<Schemas['GoldenSearchEvidenceCandidate'], 'point'> & { point: Point };
/** `incomplete` when the budget stopped the stage before every candidate's evidence ran. */
export type EvidenceView = Omit<Schemas['GoldenSearchEvidenceView'], 'candidates'> & { candidates: EvidenceCandidate[] };

export type ExamCheck = Schemas['GoldenSearchExamCheck'];
/**
 * `retention` is descriptive only, never a check; `weakness` is what approving
 * needs the owner to acknowledge, in the acknowledgement's words — empty
 * before an outcome and when the evidence meets the rules.
 */
export type ExamView = Omit<Schemas['GoldenSearchExamView'], 'candidate_point'> & { candidate_point: Point };

/** The canonical point Deploy applies, without `symbol`. */
export type DeployOffer = Omit<Schemas['GoldenSearchDeployHandoff'], 'parameters'> & { parameters: Point };
export type QualificationView = Omit<Schemas['GoldenSearchQualificationView'], 'deploy'> & { deploy: DeployOffer | null };

export interface StudyResults {
  search: SearchView | null;
  recent: ProcedureView | null;
  validation: ValidationView | null;
  evidence: EvidenceView | null;
  exam: ExamView | null;
  qualification: QualificationView | null;
}

/** The scope line every step shows above its comparisons; `data_source` is the footer's sentence. */
export type StudyScope = Schemas['GoldenSearchScope'];

/**
 * One study as the workbench reads it. `protocol` is the frozen plan echo;
 * `exposure_preview` is what opening the final test would record, while a
 * candidate is chosen (null otherwise).
 */
export type StudyDetail = Omit<Schemas['GoldenSearchStudyDetail'], 'protocol' | 'results' | 'action_refusals'> & {
  protocol: ProtocolRequest;
  results: StudyResults;
  action_refusals: Partial<Record<StudyCommandName, string>>;
};

// ---------------------------------------------------------------- commands

interface CommandEnvelope<C extends StudyCommandName, P> {
  command: C;
  expected_revision: number;
  idempotency_key: string;
  payload: P;
}

export type StudyCommandRequest =
  | CommandEnvelope<'continue', Record<string, never>>
  | CommandEnvelope<'select_candidate', { candidate_key: CandidateKey }>
  | CommandEnvelope<'open_exam', { acknowledge_final_test: true }>
  | CommandEnvelope<
      'approve',
      { note: string; acknowledge_missing_parity: true; acknowledge_research_weakness: boolean; expected_default_qualification_id: string | null }
    >
  | CommandEnvelope<'retain', { kind: RetainKind; note: string }>
  | CommandEnvelope<'close', { note: string }>
  | CommandEnvelope<'cancel', Record<string, never>>
  | CommandEnvelope<'finish', Record<string, never>>
  | CommandEnvelope<'revise', { protocol: ProtocolRequest }>;

/** A command without the envelope fields the caller fills from the study it is acting on. */
export type StudyCommand = { [C in StudyCommandRequest as C['command']]: Pick<C, 'command' | 'payload'> }[StudyCommandRequest['command']];

export interface CreateStudyRequest {
  protocol: ProtocolRequest;
  idempotency_key: string;
}

// ---------------------------------------------------------------- reads

export interface StudyListFilters {
  strategy_key?: string;
  symbol?: string;
  include_hidden?: boolean;
  limit?: number;
}

export interface EvaluationQuery {
  stage?: string;
  fold_index?: number;
  page: number;
  page_size: number;
}

export type EvaluationRow = Omit<Schemas['GoldenSearchEvaluationRow'], 'point'> & { point: Point };
export type EvaluationPage = Omit<Schemas['GoldenSearchEvaluationPage'], 'rows'> & { rows: EvaluationRow[] };

export type DailyEquityPoint = Schemas['GoldenSearchEquityPoint'];
export type DrawdownPoint = Schemas['GoldenSearchDrawdownPoint'];
/** `return_fraction` is null when the month started without positive equity to divide by. */
export type MonthlyResult = Schemas['GoldenSearchMonthlyResult'];
/** `pnl` is the price change times filled quantity, before fees. */
export type CandidateTrade = Schemas['GoldenSearchTrade'];
/** `value` is a fraction of starting capital. */
export type CumulativeReturnPoint = Schemas['GoldenSearchCumulativeReturnPoint'];
export type CandidateRunDetail = Schemas['GoldenSearchRunDetail'];

/** `GET /studies/{id}/candidates/{key}`: the development detail run, plus the exam's once it ran. */
export type CandidateDetail = Omit<Schemas['GoldenSearchCandidateDetail'], 'point'> & { point: Point };

/** What the research said when the version was approved; preserved forever, never upgraded by approval. */
export type QualificationResearch = Schemas['GoldenQualificationResearch'];

/**
 * `GET /api/research/golden-qualifications/{id}/deploy-offer`; `parameters` is
 * canonical without `symbol`. Only a `ready` status may be applied;
 * `unverifiable` means its status could not be read.
 */
export type QualificationDeployOffer = Omit<Schemas['GoldenQualificationDeployOffer'], 'parameters'> & { parameters: Point };
export type QualificationStatus = QualificationDeployOffer['status'];

/** Statuses whose stage run still owns (or is waiting for) a worker. */
export const LIVE_STATUSES: readonly PresentedStatus[] = ['queued', 'running'];

export function isLive(status: PresentedStatus): boolean {
  return LIVE_STATUSES.includes(status);
}
