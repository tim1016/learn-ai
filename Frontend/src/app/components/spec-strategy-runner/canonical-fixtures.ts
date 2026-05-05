import { StrategySpec } from '../../graphql/spec-strategy-types';

/**
 * Canonical Phase-1 spec fixtures shipped with the package.
 *
 * Mirrors the JSON files at
 * `PythonDataService/app/engine/strategy/spec/fixtures/`. Bundled here as
 * TS constants so the runner UI can offer them as picker options
 * without needing a separate fixture-list endpoint. The Python tests
 * exercise the same fixtures by path (the `_parity_helpers.py` loader),
 * so divergence between these constants and the JSON files is caught
 * in CI as a parity regression.
 *
 * If any fixture changes upstream, update this file in the same PR.
 */
export interface CanonicalFixture {
  /** Stable id used by the picker. Matches the JSON filename stem. */
  readonly id: string;
  /** Short label for the dropdown. */
  readonly label: string;
  readonly spec: StrategySpec;
}

const SPY_EMA_CROSSOVER: StrategySpec = {
  schema_version: '1.0',
  name: 'SPY EMA(5)/EMA(10) Crossover with RSI gate',
  description:
    'LEAN-pinned reference. 15-min consolidator on SPY; entry on fresh EMA5>EMA10 cross with gap >= 0.20 and 50<=RSI<=70; exit after 5 consolidated bars.',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [
    { id: 'ema5', kind: 'EMA', period: 5, source: 'close' },
    { id: 'ema10', kind: 'EMA', period: 10, source: 'close' },
    { id: 'rsi14', kind: 'RSI', period: 14, source: 'close', ma_type: 'wilders' },
  ],
  entry: {
    logic: 'AND',
    conditions: [
      { kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' },
      {
        kind: 'IndicatorComparison',
        left: {
          kind: 'Subtract',
          left: { kind: 'IndicatorRef', indicator: 'ema5' },
          right: { kind: 'IndicatorRef', indicator: 'ema10' },
        },
        op: '>=',
        right: { kind: 'Const', value: 0.2 },
      },
      { kind: 'IndicatorBetween', indicator: 'rsi14', lo: 50, hi: 70, inclusive: true },
    ],
    size: { kind: 'SetHoldings', fraction: 1.0 },
    pyramiding: 1,
  },
  position: { kind: 'EQUITY_LONG' },
  survival: [],
  exit: {
    logic: 'OR',
    conditions: [{ kind: 'BarsSinceEntry', op: '>=', value: 5 }],
  },
  diagnostics: { snapshot_at_entry: ['ema5', 'ema10', 'rsi14'] },
};

const SMA_CROSSOVER: StrategySpec = {
  schema_version: '1.0',
  name: 'SMA(10)/SMA(30) Crossover',
  description:
    'Long-only golden-cross / death-cross. Entry: fresh up-cross of short SMA over long SMA. Exit: fresh down-cross. Defaults match SmaCrossoverAlgorithm.',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [
    { id: 'sma_s', kind: 'SMA', period: 10, source: 'close' },
    { id: 'sma_l', kind: 'SMA', period: 30, source: 'close' },
  ],
  entry: {
    logic: 'AND',
    conditions: [
      { kind: 'FreshCross', left: 'sma_s', right: 'sma_l', direction: 'up' },
    ],
    size: { kind: 'SetHoldings', fraction: 1.0 },
    pyramiding: 1,
  },
  position: { kind: 'EQUITY_LONG' },
  survival: [],
  exit: {
    logic: 'OR',
    conditions: [
      { kind: 'FreshCross', left: 'sma_s', right: 'sma_l', direction: 'down' },
    ],
  },
  diagnostics: { snapshot_at_entry: ['sma_s', 'sma_l'] },
};

const RSI_MEAN_REVERSION: StrategySpec = {
  schema_version: '1.0',
  name: 'RSI(14) Mean Reversion',
  description:
    'Long-only RSI threshold mean reversion. Entry: RSI strictly below oversold. Exit: RSI strictly above overbought. Defaults match RsiMeanReversionAlgorithm.',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [
    { id: 'rsi', kind: 'RSI', period: 14, source: 'close', ma_type: 'wilders' },
  ],
  entry: {
    logic: 'AND',
    conditions: [
      {
        kind: 'IndicatorComparison',
        left: { kind: 'IndicatorRef', indicator: 'rsi' },
        op: '<',
        right: { kind: 'Const', value: 30 },
      },
    ],
    size: { kind: 'SetHoldings', fraction: 1.0 },
    pyramiding: 1,
  },
  position: { kind: 'EQUITY_LONG' },
  survival: [],
  exit: {
    logic: 'OR',
    conditions: [
      {
        kind: 'IndicatorComparison',
        left: { kind: 'IndicatorRef', indicator: 'rsi' },
        op: '>',
        right: { kind: 'Const', value: 70 },
      },
    ],
  },
  diagnostics: { snapshot_at_entry: ['rsi'] },
};

export const CANONICAL_FIXTURES: readonly CanonicalFixture[] = [
  { id: 'spy_ema_crossover', label: 'SPY EMA(5)/EMA(10) Crossover (LEAN-pinned)', spec: SPY_EMA_CROSSOVER },
  { id: 'sma_crossover', label: 'SMA(10)/SMA(30) Crossover', spec: SMA_CROSSOVER },
  { id: 'rsi_mean_reversion', label: 'RSI(14) Mean Reversion', spec: RSI_MEAN_REVERSION },
];
