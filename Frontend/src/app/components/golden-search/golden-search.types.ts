/**
 * Wire shapes for Golden Search (#2696), hand-written to mirror
 * PythonDataService/app/schemas/golden_search.py until generated types
 * replace them. Every temporal value is int64 ms UTC; interval boundaries are
 * ET-midnight instants with exclusive ends. Every number shown is computed in
 * Python — these types carry it, the browser never derives it.
 */

import type { FillModeName } from '../../models/fill-mode';
import type { RankingMeasure } from '../grid-search/grid-search.types';
import type { FoldPlan, Verdict } from '../walk-forward-study/walk-forward-study.types';

export type { RankingMeasure } from '../grid-search/grid-search.types';
export type { FoldPlan } from '../walk-forward-study/walk-forward-study.types';

/** One canonical parameter value: a point is a dict of JSON scalars (integers as ints, decimals quantized). */
export type PointValue = string | number | boolean | null;
/** A complete canonical parameter assignment, `symbol` included. */
export type Point = Readonly<Record<string, PointValue>>;

// ---------------------------------------------------------------- capabilities

export type KnobKind = 'integer' | 'decimal';

export interface CapabilityKnob {
  name: string;
  label: string;
  unit: string;
  kind: KnobKind;
  domain_low: number;
  domain_high: number;
  quantum: number;
  default_low: number;
  default_high: number;
  neighbor_step: number;
  /** The smallest step a searched knob starts with; a multiple of `quantum`. */
  default_step: number;
  searchable_by_default: boolean;
  warmup_dependent: boolean;
  default_value: number;
}

export interface FixedControl {
  label: string;
  value: string;
  reason: string;
}

export interface KnobConstraint {
  left: string;
  op: '<';
  right: string;
  message: string;
}

export type KnobPair = readonly [string, string];

export interface StrategyCapability {
  strategy_key: string;
  display_name: string;
  available: boolean;
  reason: string | null;
  knobs: CapabilityKnob[];
  fixed: FixedControl[];
  constraints: KnobConstraint[];
  default_pair_audits: KnobPair[];
}

// ---------------------------------------------------------------- protocol

export type GoldenSearchMethod = 'zoom' | 'grid';
export type KnobMode = 'search' | 'fixed';

export interface KnobPlan {
  name: string;
  mode: KnobMode;
  /** Search range, used when `mode` is `search`. */
  low: number;
  high: number;
  /** Used when `mode` is `fixed`. */
  fixed_value: number;
  /**
   * The smallest step, required for every searched knob (a multiple of the
   * declared quantum): Grid samples `low..high` at it, and Zoom stops
   * refining the knob once a round's spacing would fall below it.
   */
  step: number | null;
}

export interface SelectionPolicy {
  objective: RankingMeasure;
  min_trades: number;
  /** Fraction of peak equity, in (0, 1]. */
  max_drawdown_ceiling: number;
  require_positive_net: boolean;
}

export interface ZoomSettings {
  points: number;
  refinements: number;
  passes: number;
}

export interface ExecutionAssumptions {
  fill_mode: FillModeName;
  commission_per_order: number;
  slippage_per_share: number;
  initial_cash: number;
}

export interface StressScenario {
  key: string;
  label: string;
  slippage_add: number;
  commission_add: number;
  fill_mode: FillModeName | null;
}

export interface IncumbentRef {
  source: 'registry' | 'qualification';
  qualification_id: string | null;
  params: Point;
}

/** Mirrors `GoldenSearchProtocol` field for field; `seed` omitted defaults to the incumbent's params. */
export interface ProtocolRequest {
  strategy_key: string;
  symbol: string;
  method: GoldenSearchMethod;
  /** Every declared knob, in search order. */
  knobs: KnobPlan[];
  seed?: Point | null;
  incumbent: IncumbentRef;
  policy: SelectionPolicy;
  zoom: ZoomSettings;
  development_start_ms: number;
  development_end_ms: number;
  final_start_ms: number;
  final_end_ms: number;
  training_months: number;
  test_months: number;
  recent_window: boolean;
  pair_audits: KnobPair[];
  neighbor_audit: boolean;
  stress: StressScenario[];
  execution: ExecutionAssumptions;
  exam_min_trades: number;
  budget_cap: number;
}

export interface ProtocolRefusal {
  code: string;
  field: string | null;
  message: string;
}

// ---------------------------------------------------------------- exposure, defaults, preflight

export type ExposureState = 'not_opened' | 'previously_used' | 'history_unknown';
export type ExposureClaim = 'confirmatory' | 'exploratory';

export interface ExposureView {
  state: ExposureState;
  ledger_overlaps: number;
  outside_activity_overlaps: number;
  explanation: string;
}

/** `GET /defaults`: a full ProtocolRequest prefilled, plus the incumbent's name and the proposed final interval's exposure. */
export interface GoldenSearchDefaults extends ProtocolRequest {
  incumbent_label: string;
  exposure: ExposureView;
}

export interface StageEstimate {
  stage: string;
  label: string;
  max_evaluations: number;
}

export interface StudyEstimate {
  stages: StageEstimate[];
  total_max: number;
  reserved_for_exam_and_proof: number;
  budget_cap: number;
  serial_seconds_low: number;
  serial_seconds_high: number;
}

export interface RunUpView {
  required_samples: number;
  run_up_sessions: number;
  data_start_ms: number;
}

/** `POST /preflight`: protocol problems are refusals in the body, never a 400. */
export interface GoldenSearchPreflight {
  refusals: ProtocolRefusal[];
  estimate: StudyEstimate | null;
  folds: FoldPlan[];
  exposure: ExposureView | null;
  run_up: RunUpView | null;
}

// ---------------------------------------------------------------- study lifecycle

export type StudyState =
  | 'locked'
  | 'search_running'
  | 'awaiting_validation'
  | 'validation_running'
  | 'awaiting_candidate'
  | 'candidate_locked'
  | 'exam_running'
  | 'awaiting_review'
  | 'qualification_pending'
  | 'approved'
  | 'qualification_failed'
  | 'retained'
  | 'closed';

/** The fence status of the current stage run as the lifecycle presents it (`running` with no live job reads `interrupted`). */
export type PresentedStatus = 'idle' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';

export type StudyCommandName = 'continue' | 'select_candidate' | 'open_exam' | 'approve' | 'retain' | 'close' | 'cancel' | 'finish' | 'revise';

export type CandidateKey = 'incumbent' | 'all_period' | 'recent';
export type RetainKind = 'keep_current' | 'wait_for_fresh_data' | 'retain_exploration';
export type ExamOutcome = 'meets_rules' | 'does_not_meet_rules' | 'not_enough_evidence' | 'could_not_evaluate';

export interface StudySummary {
  id: string;
  parent_study_id: string | null;
  strategy_key: string;
  symbol: string;
  state: StudyState;
  presented_status: PresentedStatus;
  revision: number;
  created_at_ms: number;
  updated_at_ms: number;
  protocol_hash: string;
  method: GoldenSearchMethod;
  consumed_evaluations: number;
  budget_cap: number;
  cache_hits: number;
  invalid_points: number;
  incomplete: boolean;
  failure_reason: string | null;
  hidden: boolean;
  exposure_claim: ExposureClaim | null;
  exam_outcome: ExamOutcome | null;
  qualification_id: string | null;
}

export interface StudyReceiptSummary {
  data_start_ms: number;
  development_start_ms: number;
  development_end_ms: number;
  final_start_ms: number;
  final_end_ms: number;
  run_up_sessions: number;
  snapshot_digest: string;
  code: { git_revision: string; tree_state: string };
  program_version: string | null;
}

export interface StudyGuidance {
  headline: string;
  detail: string;
}

export interface StudyProgress {
  stage: string;
  completed: number;
  total_max: number;
}

export interface StageDispatch {
  job_type: 'golden_search';
  payload: { study_id: string; stage_token: string };
}

export interface StudyDecision {
  kind: string;
  note: string;
  at_ms: number;
}

export interface Metrics {
  status: 'completed' | 'failed';
  total_trades: number;
  net_profit: number | null;
  /** Fractions, as the engine reports them. */
  total_return_pct: number | null;
  sharpe_ratio: number | null;
  max_drawdown_pct: number | null;
  win_rate: number | null;
  error: string | null;
}

export type StopReason = 'no_improvement' | 'pass_limit' | 'budget' | 'quantization_limit' | 'no_eligible';

/** One refinement round of one knob; tuples arrive as two-element arrays. */
export interface ZoomRound {
  pass_index: number;
  knob: string;
  round_index: number;
  low: number;
  high: number;
  values: number[];
  /** Constraint-skipped candidate values, never evaluated: `[value, reason]`. */
  invalid: [number, string][];
  /** `[value, ineligibility code or null when eligible]`. */
  results: [number, string | null][];
  /** The objective each evaluated value scored (null when undefined), in `results` order. */
  objectives: [number, number | null][];
  chosen: number;
  moved: boolean;
  current_before: number;
  /** True on the round where the knob's spacing reached its smallest step. */
  quantization_limit: boolean;
}

/** One searched knob's outcome over the whole procedure, authored in Python from its rounds. */
export interface KnobSummary {
  knob: string;
  label: string;
  unit: string;
  start_value: number;
  retained_value: number;
  moved: boolean;
  stop_reason: StopReason;
  /** From a closed copy map, e.g. "No better tested move", "Minimum step reached". */
  stop_explanation: string;
}

export interface ProcedureCounts {
  evaluated: number;
  cached: number;
  invalid: number;
}

export interface ProcedureView {
  winner: Point;
  winner_hash: string;
  winner_metrics: Metrics | null;
  stop_reason: StopReason;
  stop_explanation: string;
  rounds: ZoomRound[];
  edge_hits: string[];
  evaluations: number;
  incomplete: boolean;
  knob_summary: KnobSummary[];
  counts: ProcedureCounts;
  passes_completed: number;
  /** The all-period search's pair landscapes, centered on its winner; the recent procedure has none. */
  pair_maps?: PairMap[];
}

export type FoldRunStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface ValidationFold extends FoldPlan {
  status: FoldRunStatus;
  winner: Point | null;
  winner_hash: string | null;
  train_metrics: Metrics | null;
  test_metrics: Metrics | null;
  /** The frozen incumbent on the same test window: the benchmark every fold is read against. */
  incumbent_test_metrics: Metrics | null;
  failure_reason: string | null;
}

/** `compute_verdict(...).as_dict()`: the legacy five-label summary with its coverage. */
export interface ValidationVerdict extends Verdict {
  retention_threshold: number;
}

export interface LinkedFoldReturn {
  fold_index: number;
  test_end_ms: number;
  /** Growth of 1 linked across test folds, minus 1; null once a fold is missing (the line breaks). */
  linked_return: number | null;
}

export interface ValidationSummaryPills {
  /** Python-authored, e.g. "5 of 6 folds judged". */
  judged: string;
  test_trades: number;
  median_retention: number | null;
}

export interface ValidationView {
  folds: ValidationFold[];
  verdict: ValidationVerdict | null;
  linked: LinkedFoldReturn[];
  /** The frozen incumbent's test returns linked the same way. */
  incumbent_linked: LinkedFoldReturn[];
  summary_pills: ValidationSummaryPills;
  explanation: string;
}

export type EvidenceCellStatus = 'tested' | 'invalid' | 'failed' | 'outside_domain' | 'untested';

export interface NeighborRow {
  value: number;
  status: EvidenceCellStatus;
  metrics: Metrics | null;
}

export interface CandidateNeighborhood {
  knob: string;
  rows: NeighborRow[];
}

export interface StressResult {
  scenario: string;
  label: string;
  metrics: Metrics | null;
}

export interface EvidenceCandidate {
  key: CandidateKey;
  label: string;
  point: Point;
  point_hash: string;
  same_as: CandidateKey[];
  development_metrics: Metrics | null;
  eligible: boolean;
  ineligibility: string | null;
  neighbors: CandidateNeighborhood[];
  stress: StressResult[];
  /** From a closed copy map keyed by the candidate's situation. */
  guidance: { title: string; text: string };
  flags: Finding[];
  /** Python-authored, e.g. "Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars". */
  params_sentence: string;
  fixed_sentence: string;
  /** False for the incumbent: keeping the current settings needs no final test. */
  exam_eligible: boolean;
}

export interface PairMapCell {
  x: number;
  y: number;
  status: EvidenceCellStatus;
  metrics: Metrics | null;
}

export interface PairMap {
  x_knob: string;
  y_knob: string;
  x_values: number[];
  y_values: number[];
  cells: PairMapCell[];
}

export interface Finding {
  code: string;
  text: string;
}

export interface Recommendation {
  headline: string;
  findings: Finding[];
}

export interface IntervalMs {
  start_ms: number;
  end_ms: number;
}

export interface EvidenceScope {
  window: IntervalMs;
  capital: number;
  costs: Pick<ExecutionAssumptions, 'fill_mode' | 'commission_per_order' | 'slippage_per_share'>;
}

export interface EvidenceView {
  candidates: EvidenceCandidate[];
  pair_maps: PairMap[];
  recommendation: Recommendation;
  scope: EvidenceScope;
}

export interface ExamCheck {
  code: string;
  label: string;
  status: 'pass' | 'fail' | 'not_available';
  detail: string;
}

export interface ExamView {
  candidate_key: CandidateKey;
  candidate_point: Point;
  window: IntervalMs;
  claim: ExposureClaim;
  exposure_state: ExposureState;
  outcome: ExamOutcome | null;
  checks: ExamCheck[];
  /** Descriptive only, never a check. */
  retention: number | null;
  candidate_metrics: Metrics | null;
  incumbent_metrics: Metrics | null;
}

export interface DeployOffer {
  program_key: string;
  symbol: string;
  parameters: Point;
  program_version: string | null;
}

export interface QualificationView {
  status: 'pending' | 'ready' | 'failed';
  qualification_id: string | null;
  failure_reason: string | null;
  deploy: DeployOffer | null;
}

export interface StudyResults {
  search: ProcedureView | null;
  recent: ProcedureView | null;
  validation: ValidationView | null;
  evidence: EvidenceView | null;
  exam: ExamView | null;
  qualification: QualificationView | null;
}

/** The scope line every step shows above its comparisons. */
export interface StudyScope {
  development_label_start_ms: number;
  development_end_ms: number;
  final_start_ms: number;
  final_end_ms: number;
  final_state: 'locked' | 'opened_once';
  capital: number;
  costs_sentence: string;
}

export interface StudyDetail extends StudySummary {
  /** The frozen protocol echo. */
  protocol: ProtocolRequest;
  receipt: StudyReceiptSummary;
  permitted_actions: StudyCommandName[];
  action_refusals: Partial<Record<StudyCommandName, string>>;
  guidance: StudyGuidance;
  progress: StudyProgress | null;
  dispatch: StageDispatch | null;
  results: StudyResults;
  decision: StudyDecision | null;
  candidate_key: CandidateKey | null;
  exam_locked: boolean;
  scope: StudyScope;
}

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

export interface EvaluationRow {
  evaluation_key: string;
  point_hash: string;
  point: Point;
  window_start_ms: number;
  window_end_ms: number;
  scenario: string;
  detail: boolean;
  stage: string;
  fold_index: number | null;
  status: 'pending' | 'completed' | 'failed';
  attempt: number;
  retries: number;
  total_trades: number | null;
  net_profit: number | null;
  total_return_pct: number | null;
  sharpe_ratio: number | null;
  max_drawdown_pct: number | null;
  win_rate: number | null;
  error: string | null;
  created_at_ms: number;
  completed_at_ms: number | null;
}

export interface EvaluationPage {
  total: number;
  page: number;
  page_size: number;
  rows: EvaluationRow[];
}

export interface DailyEquityPoint {
  ms: number;
  equity: number;
}

export interface DrawdownPoint {
  ms: number;
  drawdown: number;
}

export interface MonthlyResult {
  month_start_ms: number;
  net_profit: number;
  return_fraction: number;
  trades: number;
}

export interface CandidateTrade {
  entry_ms: number;
  exit_ms: number;
  entry_price: number;
  exit_price: number;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  indicators: Readonly<Record<string, number | null>>;
  exit_reason: string | null;
}

export interface CumulativeReturnPoint {
  ms: number;
  /** A fraction of starting capital. */
  value: number;
}

export interface CandidateRunDetail {
  window: IntervalMs;
  metrics: Metrics;
  cumulative_return: CumulativeReturnPoint[];
  daily_equity: DailyEquityPoint[];
  drawdown: DrawdownPoint[];
  monthly: MonthlyResult[];
  trades: CandidateTrade[];
}

/** `GET /studies/{id}/candidates/{key}`: the development detail run, plus the exam's once it ran. */
export interface CandidateDetail {
  candidate_key: CandidateKey;
  point: Point;
  development: CandidateRunDetail | null;
  exam: CandidateRunDetail | null;
}

export type QualificationStatus = 'ready' | 'stale' | 'revoked';

/** `GET /api/research/golden-qualifications/{id}/deploy-offer`; `parameters` is canonical without `symbol`. */
export interface QualificationDeployOffer {
  qualification_id: string;
  program_key: string;
  program_version: string | null;
  symbol: string;
  parameters: Point;
  status: QualificationStatus;
  explanation: string;
}

/** Statuses whose stage run still owns (or is waiting for) a worker. */
export const LIVE_STATUSES: readonly PresentedStatus[] = ['queued', 'running'];

export function isLive(status: PresentedStatus): boolean {
  return LIVE_STATUSES.includes(status);
}
