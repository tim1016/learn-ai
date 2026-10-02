/**
 * Shared strategy-view fixtures (#2639).
 *
 * The indicator names and gates are made up on purpose ("Foo 7", "Bar 3"):
 * a surface that renders them proves it draws whatever the backend declares
 * and hardcodes no indicator of its own.
 *
 * Four decision bars, 15 minutes each: two the bot saw before it started at
 * 18:30:05 UTC, then two decisions (seq 11 and 12). Under the strategy's gate
 * (`g_rule`) the four bars read: up + holds, down + fails, up + no result,
 * down + holds — one of each candle fill.
 */

import type {
  CustomGate,
  DecisionExplanationView,
  GateCatalogueEntry,
  RecentDecisionView,
  StrategyViewCandle,
  StrategyViewResponse,
} from '../components/broker/v2-panel/lib/broker-v2-panel.types';
import type { IndicatorCategory } from '../shared/indicator-catalog/indicator-catalog.service';

export const STRATEGY_BAR_MS = 15 * 60_000;
/** 18:00 UTC on Wed 2026-09-30: the first fixture bar opens here. */
export const STRATEGY_FIRST_BAR_START_MS = Date.UTC(2026, 8, 30, 18, 0, 0);
export const STRATEGY_RUN_STARTED_AT_MS = Date.UTC(2026, 8, 30, 18, 30, 5);
export const STRATEGY_RUN_STOPPED_AT_MS = Date.UTC(2026, 8, 30, 19, 59, 0);

export function barCloseMs(index: number): number {
  return STRATEGY_FIRST_BAR_START_MS + (index + 1) * STRATEGY_BAR_MS;
}

/** One bar's recorded values and checks, each sentence tagged with the bar index. */
export function fakeExplanation(index: number, overrides: Partial<DecisionExplanationView> = {}): DecisionExplanationView {
  return {
    ready: true,
    holding: false,
    signal: 'HOLD',
    values: [
      { key: 'foo', label: 'Foo 7', value: index === 0 ? null : 100 + index, text: index === 0 ? 'not available' : `${100 + index}.00` },
      { key: 'bar', label: 'Bar 3', value: 50 + index, text: `${50 + index}.0` },
      { key: 'baz', label: 'Baz 1', value: 1, text: '1.0' },
    ],
    checks: [
      {
        check_id: 'quit', role: 'exit', label: 'Quit', chip: 'quit in 3',
        observed_text: '3 bars left', needs: 'at 0 bars left', passed: false, applies: false,
      },
      {
        check_id: 'zap', role: 'entry', label: 'Zap', chip: 'zap',
        observed_text: `no zap at bar ${index}`, needs: 'a zap up', passed: false, applies: true,
      },
      {
        check_id: 'bar_band', role: 'entry', label: 'Bar band', chip: `Bar ${50 + index}.0`,
        observed_text: `${50 + index}.0`, needs: 'in 20–80', passed: true, applies: true,
      },
    ],
    ...overrides,
  };
}

export function fakeStrategyCandle(index: number, overrides: Partial<StrategyViewCandle> = {}): StrategyViewCandle {
  return {
    bar_start_ms: barCloseMs(index) - STRATEGY_BAR_MS,
    bar_close_ms: barCloseMs(index),
    open: 500,
    high: 503,
    low: 498,
    close: 501,
    volume: 1_000,
    phase: 'decision',
    phase_text: null,
    outcome: null,
    reason_code: null,
    decision_seq: null,
    explanation: fakeExplanation(index),
    gates: { g_rule: true, g_mine: false },
    ...overrides,
  };
}

export const BEFORE_START_TEXT = 'Before start · not acted on';

export function fakeStrategyView(overrides: Partial<StrategyViewResponse> = {}): StrategyViewResponse {
  return {
    strategy_key: 'foo_cross',
    strategy_name: 'Foo Cross',
    symbol: 'SPY',
    decision_timeframe_ms: STRATEGY_BAR_MS,
    run_id: 'run-7',
    run_started_at_ms: STRATEGY_RUN_STARTED_AT_MS,
    run_stopped_at_ms: null,
    declaration: {
      values: [
        { key: 'foo', label: 'Foo 7', variable: 'FOO7', pane: 'price', band: null, decimals: 2, catalogue: null },
        { key: 'bar', label: 'Bar 3', variable: 'BAR3', pane: 'osc', band: [20, 80], decimals: 1, catalogue: null },
        { key: 'baz', label: 'Baz 1', variable: 'BAZ1', pane: null, band: null, decimals: 1, catalogue: null },
      ],
      gates: [
        { gate_id: 'g_mine', label: 'Foo over close', expression: 'FOO7 − close > 0', source: 'mine' },
        { gate_id: 'g_rule', label: 'Bar 3 in 20–80', expression: '20 ≤ BAR3 ≤ 80', source: 'strategy' },
      ],
      default_gate_id: 'g_rule',
    },
    candles: [
      fakeStrategyCandle(0, {
        phase: 'before_start', phase_text: BEFORE_START_TEXT, open: 500, close: 502,
        gates: { g_rule: true, g_mine: false },
      }),
      fakeStrategyCandle(1, {
        phase: 'before_start', phase_text: BEFORE_START_TEXT, open: 502, close: 499,
        gates: { g_rule: false, g_mine: true },
      }),
      fakeStrategyCandle(2, {
        open: 499, close: 500, outcome: 'no_action', reason_code: 'NO_ZAP', decision_seq: 11,
        gates: { g_rule: null, g_mine: true },
      }),
      fakeStrategyCandle(3, {
        open: 500, close: 497, outcome: 'enter_intent', reason_code: 'ZAP_UP', decision_seq: 12,
        explanation: fakeExplanation(3, { signal: 'ENTER' }),
        gates: { g_rule: true, g_mine: false },
      }),
    ],
    unexplained_decision_count: 0,
    notices: [],
    settings: { foo_length: 7, bar_low: 20, fast: true },
    ...overrides,
  };
}

/** The panel's receipt for one of the fixture's decision bars. */
export function fakeRecentDecision(
  seq: number,
  barIndex: number,
  overrides: Partial<RecentDecisionView> = {},
): RecentDecisionView {
  return {
    seq,
    recorded_at_ms: barCloseMs(barIndex) + 1_200,
    outcome: 'no_action',
    reason_code: 'NO_ZAP',
    bar_ref: `SPY@${barCloseMs(barIndex)}`,
    order_ref: null,
    simulated: false,
    authority_account_id: 'PA9',
    authority_kind: 'real_paper',
    decision_bar_close_ms: barCloseMs(barIndex),
    explanation: fakeExplanation(barIndex),
    ...overrides,
  };
}

/** A custom gate the viewer saved on the fixture's strategy. */
export function fakeCustomGate(overrides: Partial<CustomGate> = {}): CustomGate {
  return {
    gate_id: 'g-0123456789ab',
    strategy_key: 'foo_cross',
    label: 'Foo above close',
    expression: 'FOO7 - close',
    sign: 'gt',
    terms: [{ coefficient: 1, variable: 'FOO7' }, { coefficient: -1, variable: 'close' }],
    constant: 0,
    created_at_ms: STRATEGY_FIRST_BAR_START_MS,
    updated_at_ms: STRATEGY_FIRST_BAR_START_MS,
    ...overrides,
  };
}

/** A small chart catalogue: one length-only indicator, one with no setting, one a gate cannot use. */
export const FAKE_INDICATOR_CATALOGUE: IndicatorCategory[] = [
  {
    name: 'Overlap',
    indicators: [
      {
        name: 'ema', category: 'Overlap', description: 'Exponential moving average',
        configurable_params: [{ name: 'length', type: 'int', default: 10, min: 1, max: 500, description: 'Bars' }],
      },
      { name: 'vwap', category: 'Overlap', description: 'Volume-weighted average price', configurable_params: [] },
    ],
  },
  {
    name: 'Volume',
    indicators: [
      { name: 'obv', category: 'Volume', description: 'On-balance volume', configurable_params: [] },
    ],
  },
  {
    name: 'Momentum',
    indicators: [
      {
        name: 'macd', category: 'Momentum', description: 'Moving average convergence divergence',
        configurable_params: [
          { name: 'fast', type: 'int', default: 12, min: 1, max: 100, description: 'Fast' },
          { name: 'slow', type: 'int', default: 26, min: 1, max: 200, description: 'Slow' },
        ],
      },
    ],
  },
];

/** What the data plane says a gate can read from the catalogue above: EMA at its default length, and VWAP. */
export const FAKE_GATE_CATALOGUE: GateCatalogueEntry[] = [
  {
    name: 'ema', description: 'Exponential moving average', variable: 'EMA10',
    default_length: 10, min_length: 1, max_length: 500,
  },
  {
    name: 'vwap', description: 'Volume-weighted average price', variable: 'VWAP',
    default_length: null, min_length: null, max_length: null,
  },
];
