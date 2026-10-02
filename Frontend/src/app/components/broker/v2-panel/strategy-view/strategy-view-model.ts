import type { CandlestickData, SeriesMarker, UTCTimestamp } from 'lightweight-charts';

import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import {
  CHART_SERIES_COLOR_TOKENS,
  type ChartSeriesColorToken,
} from '../../../../shared/trading-chart';
import type {
  ExplainedCheckView,
  ExplainedValueView,
  RecentDecisionView,
  StrategyViewCandle,
  StrategyViewDeclarationView,
  StrategyViewGateView,
  StrategyViewResponse,
} from '../lib/broker-v2-panel.types';

/**
 * What the strategy view draws, derived from one backend read (#2639).
 *
 * Nothing here judges a bar: a candle is bright exactly where the backend
 * says the active gate held, every label and sentence is the backend's, and
 * a drawn line is the bot's recorded value at that bar. The browser only
 * picks colours, panes and order.
 */

/** Dark Bright Gate candle colours: hue is the candle's direction, shade is
 * the gate's result — bright where it holds, dark where it fails or has no
 * result. Canvas needs literal colours, so the legend binds these too. */
export const GATE_CANDLE_COLORS = {
  upBright: '#22c2a8',
  upDark: '#123f3a',
  upDarkEdge: '#1f6b61',
  downBright: '#f4534f',
  downDark: '#4f1f1f',
  downDarkEdge: '#8c3532',
} as const;

const DECISION_MARKER_COLORS = { ENTER: '#7aa9ff', EXIT: '#ff9800' } as const;
/** A value's band: the dashed reference lines the market tape draws too. */
export const BAND_LINE_COLOR = '#4a5068';

/** Drawn values take colours in declaration order: the first three as the
 * owner-approved mockup draws them, then the rest of the series palette. */
const LINE_COLOR_ORDER: readonly ChartSeriesColorToken[] = [
  'series-amber',
  'series-blue',
  'series-violet',
  'series-teal',
  'series-pink',
  'series-orange',
  'series-sky',
  'series-green',
  'series-purple',
  'series-red',
];

const PRICE_PANE = 'price';

/** A strategy-view read that failed, in the backend's words. */
export interface StrategyViewFailure {
  readonly message: string;
  readonly why: string | null;
}

/** Whether the gate held on this bar: `null` when the backend could not judge it. */
export type GateResult = boolean | null;

export interface CandleFill {
  readonly body: string;
  readonly edge: string;
}

export interface StrategyLinePoint {
  readonly time: UTCTimestamp;
  readonly value: number;
}

/** One drawn value: its pane, colour token, optional band and points. */
export interface StrategyLinePlan {
  readonly key: string;
  readonly label: string;
  /** 0 is the candles' pane; values sharing a pane id share a pane. */
  readonly paneIndex: number;
  readonly color: ChartSeriesColorToken;
  readonly band: readonly [number, number] | null;
  readonly points: readonly StrategyLinePoint[];
}

/** The chart library's seconds-UTC time for a candle labelled by its close. */
export function toChartTime(ms: number): UTCTimestamp {
  return Math.floor(ms / 1000) as UTCTimestamp;
}

export function gateResult(candle: StrategyViewCandle, gateId: string): GateResult {
  return candle.gates[gateId] ?? null;
}

export function gateCandleFill(candle: StrategyViewCandle, gateId: string): CandleFill {
  const up = candle.close >= candle.open;
  if (gateResult(candle, gateId) === true) {
    const bright = up ? GATE_CANDLE_COLORS.upBright : GATE_CANDLE_COLORS.downBright;
    return { body: bright, edge: bright };
  }
  return up
    ? { body: GATE_CANDLE_COLORS.upDark, edge: GATE_CANDLE_COLORS.upDarkEdge }
    : { body: GATE_CANDLE_COLORS.downDark, edge: GATE_CANDLE_COLORS.downDarkEdge };
}

export function toGateShadedCandles(
  candles: readonly StrategyViewCandle[],
  gateId: string,
): CandlestickData<UTCTimestamp>[] {
  return candles.map((candle) => {
    const fill = gateCandleFill(candle, gateId);
    return {
      time: toChartTime(candle.bar_close_ms),
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
      color: fill.body,
      borderColor: fill.edge,
      wickColor: fill.edge,
    };
  });
}

/** Every declared value with a pane, as a line on that pane. */
export function strategyLinePlans(
  declaration: StrategyViewDeclarationView,
  candles: readonly StrategyViewCandle[],
): StrategyLinePlan[] {
  const paneIndices = new Map<string, number>([[PRICE_PANE, 0]]);
  const plans: StrategyLinePlan[] = [];
  for (const spec of declaration.values) {
    if (spec.pane === null) continue;
    let paneIndex = paneIndices.get(spec.pane);
    if (paneIndex === undefined) {
      paneIndex = paneIndices.size;
      paneIndices.set(spec.pane, paneIndex);
    }
    plans.push({
      key: spec.key,
      label: spec.label,
      paneIndex,
      color: LINE_COLOR_ORDER[plans.length % LINE_COLOR_ORDER.length],
      band: spec.band !== null && spec.band.length === 2 ? [spec.band[0], spec.band[1]] : null,
      points: candles.flatMap((candle) => {
        const value = candle.explanation.values.find((recorded) => recorded.key === spec.key)?.value;
        return value === null || value === undefined
          ? []
          : [{ time: toChartTime(candle.bar_close_ms), value }];
      }),
    });
  }
  return plans;
}

/** The literal colour a canvas draws a line token in. */
export function lineColorHex(token: ChartSeriesColorToken): string {
  return CHART_SERIES_COLOR_TOKENS.get(token)?.hex ?? '#4d8dff';
}

/** The CSS colour a legend swatch draws a line token in. */
export function lineColorVar(token: ChartSeriesColorToken): string {
  return `var(${CHART_SERIES_COLOR_TOKENS.get(token)?.cssVar ?? '--chart-series-blue'})`;
}

/** A marker on every decision bar whose signal was to enter or exit. */
export function decisionMarkers(candles: readonly StrategyViewCandle[]): SeriesMarker<UTCTimestamp>[] {
  return candles.flatMap((candle): SeriesMarker<UTCTimestamp>[] => {
    const signal = candle.explanation.signal;
    if (candle.phase !== 'decision' || signal === 'HOLD') return [];
    return [{
      time: toChartTime(candle.bar_close_ms),
      position: signal === 'ENTER' ? 'belowBar' : 'aboveBar',
      shape: signal === 'ENTER' ? 'arrowUp' : 'arrowDown',
      color: DECISION_MARKER_COLORS[signal],
      text: formatReceiptLabel(signal),
    }];
  });
}

/** "15m", "1h", "1D", "30s": the decision bar's length for the tab label. */
export function decisionTimeframeLabel(timeframeMs: number): string {
  const units: readonly [number, string][] = [[86_400_000, 'D'], [3_600_000, 'h'], [60_000, 'm']];
  for (const [unitMs, suffix] of units) {
    if (timeframeMs % unitMs === 0) return `${timeframeMs / unitMs}${suffix}`;
  }
  return `${timeframeMs / 1000}s`;
}

/** Rules that could act on the bar first; the rest keep their order after. */
export function orderedChecks(checks: readonly ExplainedCheckView[]): ExplainedCheckView[] {
  return [...checks.filter((check) => check.applies), ...checks.filter((check) => !check.applies)];
}

/** "EMA 5 763.46 · EMA 10 767.39 · RSI 14 48.6": the bot's own values, worded by the backend. */
export function valuesLine(values: readonly ExplainedValueView[]): string {
  return values.map((value) => `${value.label} ${value.text}`).join(' · ');
}

export function gateResultText(result: GateResult): string {
  if (result === true) return 'holds, bright candle';
  if (result === false) return 'fails, dark candle';
  return 'no result';
}

/** The strategy's own rule first, then the viewer's gates, each in the backend's order. */
export function gatesStrategyFirst(gates: readonly StrategyViewGateView[]): StrategyViewGateView[] {
  return [...gates.filter((gate) => gate.source === 'strategy'), ...gates.filter((gate) => gate.source !== 'strategy')];
}

/** The gate the viewer chose when it still exists, else the declaration's default. */
export function resolveActiveGate(
  declaration: StrategyViewDeclarationView,
  preferredGateId: string | null,
): StrategyViewGateView | null {
  return declaration.gates.find((gate) => gate.gate_id === preferredGateId)
    ?? declaration.gates.find((gate) => gate.gate_id === declaration.default_gate_id)
    ?? declaration.gates[0]
    ?? null;
}

/** Bars shown before the run's first decision when the chart opens: one regular session of 15-minute bars. */
const OPENING_CONTEXT_BARS = 26;
/** Empty slots kept right of the last candle, so the run's end line and its label stay on screen. */
const OPENING_RIGHT_PAD_BARS = 3;

/**
 * The bar range the chart opens on: the run, with a session of the bars the
 * bot evaluated before it, rather than every warmup day squeezed to one side.
 * `null` when there is nothing to show.
 */
export function openingRange(
  candles: readonly StrategyViewCandle[],
  startedAtMs: number | null,
): { from: number; to: number } | null {
  if (candles.length === 0) return null;
  const firstAfterStart = startedAtMs === null ? -1 : candles.findIndex((candle) => candle.bar_close_ms > startedAtMs);
  const anchor = firstAfterStart === -1 ? candles.length - 1 : firstAfterStart;
  return { from: Math.max(0, anchor - OPENING_CONTEXT_BARS), to: candles.length - 1 + OPENING_RIGHT_PAD_BARS };
}

/** The visible bar range, moved just enough to bring bar `index` into view; `null` when it already is. */
export function rangeRevealing(
  range: { from: number; to: number },
  index: number,
): { from: number; to: number } | null {
  if (index >= range.from && index <= range.to) return null;
  const half = (range.to - range.from) / 2;
  return { from: index - half, to: index + half };
}

export function candleAt(
  candles: readonly StrategyViewCandle[],
  barCloseMs: number | null,
): StrategyViewCandle | null {
  if (barCloseMs === null) return null;
  return candles.find((candle) => candle.bar_close_ms === barCloseMs) ?? null;
}

// ── Decisions beside the chart ───────────────────────────────────────────────

/** One row of the decisions list: a recorded decision, or the newest bar the
 * bot evaluated before it started. */
export interface DecisionRowView {
  readonly key: string;
  readonly timeMs: number;
  /** A bar close lands on a minute; a receipt's own time keeps its seconds. */
  readonly granularity: 'minute' | 'time';
  /** The candle this row selects, when it has one. */
  readonly barCloseMs: number | null;
  readonly beforeStart: boolean;
  /** Backend prose for a before-start bar; rendered verbatim. */
  readonly phaseText: string | null;
  /** Raw codes, rendered through `receiptLabel`. */
  readonly outcome: string | null;
  readonly reasonCode: string | null;
  readonly authorityKind: string | null;
  /** `null` ⇒ this decision was recorded before decisions saved their values. */
  readonly explanation: StrategyViewCandle['explanation'] | null;
  /** The explanation's checks, those that could act on the bar first. */
  readonly checks: readonly ExplainedCheckView[];
}

export type DecisionListEntry =
  | { readonly kind: 'row'; readonly row: DecisionRowView }
  | { readonly kind: 'start'; readonly startedAtMs: number };

function decisionRow(decision: RecentDecisionView): DecisionRowView {
  const barCloseMs = decision.decision_bar_close_ms ?? null;
  return {
    key: `decision:${decision.seq}`,
    timeMs: barCloseMs ?? decision.recorded_at_ms,
    granularity: barCloseMs === null ? 'time' : 'minute',
    barCloseMs,
    beforeStart: false,
    phaseText: null,
    outcome: decision.outcome,
    // "No action · No action" says one thing twice; a reason that only
    // restates its outcome is left out.
    reasonCode: decision.reason_code.toUpperCase() === decision.outcome.toUpperCase() ? null : decision.reason_code,
    authorityKind: decision.authority_kind ?? null,
    explanation: decision.explanation ?? null,
    checks: orderedChecks(decision.explanation?.checks ?? []),
  };
}

function beforeStartRow(candle: StrategyViewCandle): DecisionRowView {
  return {
    key: `before-start:${candle.bar_close_ms}`,
    timeMs: candle.bar_close_ms,
    granularity: 'minute',
    barCloseMs: candle.bar_close_ms,
    beforeStart: true,
    phaseText: candle.phase_text ?? null,
    outcome: candle.outcome ?? null,
    reasonCode: candle.reason_code ?? null,
    authorityKind: null,
    explanation: candle.explanation,
    checks: orderedChecks(candle.explanation.checks),
  };
}

/**
 * The decisions list, oldest first, with the newest before-start bar above a
 * "Bot started" divider.
 *
 * The divider is only drawn where it is true: the panel lists a bounded
 * window of recent decisions, so once the run's first decision has scrolled
 * out of that window a divider above the oldest row would claim the bot
 * started right before a decision it did not start before.
 */
export function decisionListEntries(
  decisions: readonly RecentDecisionView[],
  view: StrategyViewResponse | null,
): DecisionListEntry[] {
  const rows = [...decisions].sort((left, right) => left.seq - right.seq).map(decisionRow);
  const startedAtMs = view?.run_started_at_ms ?? null;
  const beforeStart = view?.candles.filter((candle) => candle.phase === 'before_start').at(-1);
  if (view === null || startedAtMs === null || beforeStart === undefined || !listReachesRunStart(decisions, view)) {
    return rows.map((row) => ({ kind: 'row', row }));
  }
  const firstAfterStart = rows.findIndex((row) => row.timeMs > startedAtMs);
  const insertAt = firstAfterStart === -1 ? rows.length : firstAfterStart;
  return [
    ...rows.slice(0, insertAt).map((row): DecisionListEntry => ({ kind: 'row', row })),
    { kind: 'row', row: beforeStartRow(beforeStart) },
    { kind: 'start', startedAtMs },
    ...rows.slice(insertAt).map((row): DecisionListEntry => ({ kind: 'row', row })),
  ];
}

/** The listed decisions include the run's first decision bar (or the run has none yet). */
function listReachesRunStart(decisions: readonly RecentDecisionView[], view: StrategyViewResponse): boolean {
  const firstDecisionSeq = view.candles.find(
    (candle) => candle.phase === 'decision' && candle.decision_seq !== null && candle.decision_seq !== undefined,
  )?.decision_seq;
  if (firstDecisionSeq === null || firstDecisionSeq === undefined) return true;
  return decisions.some((decision) => decision.seq === firstDecisionSeq);
}
