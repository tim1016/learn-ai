/**
 * TypeScript types mirroring PythonDataService's `StrategySpec` Pydantic
 * schema. The Python schema is the single source of truth — see
 * `PythonDataService/app/engine/strategy/spec/schema.py` and the live
 * JSON Schema export at `GET /api/spec-strategy/schema`.
 *
 * This file exists for editor autocomplete / type-checking against the
 * shape; it does NOT validate. Validation is done by the Python layer
 * when the spec is round-tripped through the backtest endpoint.
 *
 * If the Python schema gains a new condition kind or indicator type,
 * add the corresponding member here in the same PR — the file is
 * deliberately small so this stays cheap.
 */

// ---------------------------------------------------------------------------
// Operand AST
// ---------------------------------------------------------------------------
export interface IndicatorRef {
  kind: 'IndicatorRef';
  indicator: string;
}

export interface BarField {
  kind: 'BarField';
  field: 'open' | 'high' | 'low' | 'close' | 'volume';
}

export interface ConstOperand {
  kind: 'Const';
  value: number;
}

export interface SubtractOperand {
  kind: 'Subtract';
  left: Operand;
  right: Operand;
}

export type Operand = IndicatorRef | BarField | ConstOperand | SubtractOperand;

// ---------------------------------------------------------------------------
// Indicators
// ---------------------------------------------------------------------------
export type IndicatorKind = 'SMA' | 'EMA' | 'RSI' | 'ADX' | 'MACD' | 'SUPERTREND';

export type BarSource = 'open' | 'high' | 'low' | 'close' | 'hlc3' | 'ohlc4';

export interface IndicatorBlock {
  id: string;
  kind: IndicatorKind;
  period: number;
  source?: BarSource;
  ma_type?: 'wilders' | 'simple' | null;

  // MACD-only
  fast_period?: number | null;
  signal_period?: number | null;

  // SUPERTREND-only
  multiplier?: number | null;
}

// ---------------------------------------------------------------------------
// Conditions
// ---------------------------------------------------------------------------
export type ComparisonOp = '<' | '<=' | '==' | '>=' | '>' | '!=';

export interface IndicatorComparisonCondition {
  kind: 'IndicatorComparison';
  left: Operand;
  op: ComparisonOp;
  right: Operand;
}

export interface IndicatorBetweenCondition {
  kind: 'IndicatorBetween';
  indicator: string;
  lo: number;
  hi: number;
  inclusive?: boolean;
}

export interface FreshCrossCondition {
  kind: 'FreshCross';
  left: string;
  right: string;
  direction: 'up' | 'down';
}

export interface BarsSinceEntryCondition {
  kind: 'BarsSinceEntry';
  op: ComparisonOp;
  value: number;
}

export interface TimeOfDayCondition {
  kind: 'TimeOfDay';
  after?: string | null;
  before?: string | null;
  tz?: string;
}

export interface PnLPercentCondition {
  kind: 'PnLPercent';
  op: ComparisonOp;
  value: number;
}

export interface PnLPointsCondition {
  kind: 'PnLPoints';
  op: ComparisonOp;
  value: number;
}

export interface DrawdownFromPeakCondition {
  kind: 'DrawdownFromPeak';
  value: number;
}

export interface BarPropertyCondition {
  kind: 'BarProperty';
  property: 'range' | 'body' | 'range_pct' | 'body_pct';
  op: ComparisonOp;
  value: number;
}

export type Condition =
  | IndicatorComparisonCondition
  | IndicatorBetweenCondition
  | FreshCrossCondition
  | BarsSinceEntryCondition
  | TimeOfDayCondition
  | PnLPercentCondition
  | PnLPointsCondition
  | DrawdownFromPeakCondition
  | BarPropertyCondition;

// ---------------------------------------------------------------------------
// Logic tree
// ---------------------------------------------------------------------------
export interface LogicNode {
  logic: 'AND' | 'OR';
  conditions: (Condition | LogicNode)[];
}

// ---------------------------------------------------------------------------
// Lifecycle blocks
// ---------------------------------------------------------------------------
export interface SetHoldingsSize {
  kind: 'SetHoldings';
  fraction: number;
}

export interface FixedContractsSize {
  kind: 'FixedContracts';
  value: number;
}

export type SizeRule = SetHoldingsSize | FixedContractsSize;

export interface EntryBlock {
  logic: 'AND' | 'OR';
  conditions: (Condition | LogicNode)[];
  size: SizeRule;
  pyramiding?: number;
}

export interface ExitBlock {
  logic: 'AND' | 'OR';
  conditions: (Condition | LogicNode)[];
}

export interface CloseAllAction {
  kind: 'CLOSE_ALL';
}

export type SurvivalAction = CloseAllAction;

export interface SurvivalRule {
  name: string;
  when: { logic: 'AND' | 'OR'; conditions: (Condition | LogicNode)[] };
  action: SurvivalAction;
}

// ---------------------------------------------------------------------------
// Position
// ---------------------------------------------------------------------------
export interface EquityLongPosition {
  kind: 'EQUITY_LONG';
}

export interface OptionTemplatePosition {
  kind: 'OPTION_TEMPLATE';
  template: string;
  expiration?: Record<string, unknown> | null;
  legs?: Record<string, unknown>[];
  filters?: Record<string, unknown> | null;
}

export type PositionSpec = EquityLongPosition | OptionTemplatePosition;

// ---------------------------------------------------------------------------
// Top-level
// ---------------------------------------------------------------------------
export interface Resolution {
  period_minutes: number;
}

export interface Diagnostics {
  snapshot_at_entry?: string[];
  snapshot_at_exit?: string[];
}

export interface StrategySpec {
  schema_version: '1.0';
  name: string;
  description?: string | null;
  symbols: [string]; // Phase-1 single-symbol guarantee
  resolution: Resolution;
  indicators: IndicatorBlock[];
  entry: EntryBlock;
  position?: PositionSpec;
  survival?: SurvivalRule[];
  exit: ExitBlock;
  diagnostics?: Diagnostics;
}

// ---------------------------------------------------------------------------
// Backtest response (camelCase from GraphQL → Apollo)
// ---------------------------------------------------------------------------
export interface SpecStrategyTrade {
  tradeNumber: number;
  entryTime: string;
  entryPrice: number;
  exitTime: string;
  exitPrice: number;
  indicators: Record<string, number>;
  pnlPts: number;
  pnlPct: number;
  result: 'WIN' | 'LOSS';
  signalReason: string;
}

export interface SpecStrategyBacktestResult {
  success: boolean;
  strategyName: string;
  initialCash: number;
  finalEquity: number;
  netProfit: number;
  totalFees: number;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  trades: SpecStrategyTrade[];
  logLines: string[];
  error?: string | null;
}

export interface RunSpecStrategyBacktestVariables {
  specJson: string;
  startDate: string; // YYYY-MM-DD
  endDate: string;
  initialCash?: number;
  fillMode?: 'signal_bar_close' | 'next_bar_open';
  commissionPerOrder?: number;
}

export interface RunSpecStrategyBacktestResponse {
  runSpecStrategyBacktest: SpecStrategyBacktestResult;
}
