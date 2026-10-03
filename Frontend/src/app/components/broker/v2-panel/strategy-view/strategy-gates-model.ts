import { refusalBody } from '../../../../shared/errors/refusal-body';
import type {
  CustomGate,
  CustomGateInput,
  GateCandle,
  GateCatalogueEntry,
  GateEvaluationRequest,
  GateEvaluationResponse,
  StrategyViewGateView,
  StrategyViewResponse,
} from '../lib/broker-v2-panel.types';

/**
 * Custom Dark Bright Gates on the strategy view (#2639 D8–D10).
 *
 * The data plane judges every gate; this module only shapes the request from
 * the candles the page already shows, with the view's lead-in bars for a
 * catalogue indicator to warm up on (#2800), and folds the answer back into
 * the view, so the picker, the chart and the popover treat a custom gate
 * exactly like the strategy's own rule.
 */

/** The id the data plane judges an unsaved draft under. */
export const DRAFT_GATE_ID = 'draft';

/** The candle fields every gate can read. */
const CANDLE_VARIABLES = ['open', 'high', 'low', 'close', 'volume'] as const;

/** Said under the catalogue's title, on a bot's view and Strategy Lab's alike. */
const CATALOGUE_NOTE = 'computed by the chart, not recorded by the strategy';

/** A gate's results, tied to the candles they were judged on. */
export interface GateEvaluation {
  /** The judged candles' bar closes, in the order `results` lists them. */
  readonly closes: readonly number[];
  readonly response: GateEvaluationResponse;
}

/** Each decision candle as a gate reads it: OHLCV and the bot's values by key. */
export function gateCandles(view: StrategyViewResponse): GateCandle[] {
  return view.candles.map((candle) => ({
    bar_close_ms: candle.bar_close_ms,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
    volume: candle.volume,
    values: Object.fromEntries(candle.explanation.values.map((value) => [value.key, value.value ?? null])),
  }));
}

/**
 * Judge the strategy's saved gates (and `draft`) on `view`'s candles, under its
 * deployed settings. The view's lead-in bars go along; no gate is judged on them.
 */
export function gateEvaluationRequest(
  view: StrategyViewResponse,
  draft: CustomGateInput | null = null,
): GateEvaluationRequest {
  return {
    symbol: view.symbol,
    settings: { ...(view.settings ?? {}) },
    candles: gateCandles(view),
    lead_in: view.lead_in ?? [],
    draft,
  };
}

/** "FOO7 − close > 0": the expression with the side of zero that is bright. */
export function gateExpressionText(gate: Pick<CustomGateInput, 'expression' | 'sign'>): string {
  return `${gate.expression} ${gate.sign === 'gt' ? '> 0' : '< 0'}`;
}

export function customGateView(gate: CustomGate): StrategyViewGateView {
  return { gate_id: gate.gate_id, label: gate.label, expression: gateExpressionText(gate), source: 'mine' };
}

export function draftGateView(draft: CustomGateInput): StrategyViewGateView {
  return { gate_id: DRAFT_GATE_ID, label: `Preview · ${draft.label}`, expression: gateExpressionText(draft), source: 'mine' };
}

/**
 * The view with the strategy's saved gates, and a previewed draft, listed
 * after its own rule and recorded on every candle they were judged on.
 *
 * Results are matched to candles by bar close, so an answer for an earlier
 * read never lands on the wrong bar; a candle it did not cover has no result
 * and reads dark.
 */
export function withCustomGates(
  view: StrategyViewResponse,
  saved: readonly CustomGate[],
  evaluations: readonly GateEvaluation[],
  draft: CustomGateInput | null,
  extraNotices: readonly string[] = [],
): StrategyViewResponse {
  const gates = [
    ...view.declaration.gates,
    ...saved.map(customGateView),
    ...(draft === null ? [] : [draftGateView(draft)]),
  ];
  const byClose = evaluations.map((evaluation) => ({
    index: new Map(evaluation.closes.map((close, position) => [close, position])),
    results: evaluation.response.results,
  }));
  return {
    ...view,
    declaration: { ...view.declaration, gates },
    candles: view.candles.map((candle) => {
      const judged: Record<string, boolean | null> = {};
      for (const { index, results } of byClose) {
        const position = index.get(candle.bar_close_ms);
        if (position === undefined) continue;
        for (const [gateId, column] of Object.entries(results)) judged[gateId] = column[position] ?? null;
      }
      return { ...candle, gates: { ...candle.gates, ...judged } };
    }),
    notices: [
      ...(view.notices ?? []),
      ...evaluations.flatMap((evaluation) => evaluation.response.notices ?? []),
      ...extraNotices,
    ],
  };
}

/** A name a gate can use, and what it stands for. */
export interface GateVariableChip {
  readonly name: string;
  readonly hint: string;
}

export interface GateVariableGroup {
  readonly title: string;
  /** Said under the title, e.g. that the chart computes these. */
  readonly note: string | null;
  readonly chips: readonly GateVariableChip[];
  /** Why this group offers nothing right now, said in place of its chips. */
  readonly unavailable?: string;
}

/**
 * The names a gate can use, in the order the data plane resolves them: the
 * bot's recorded values, its deployed settings, the candle, then the
 * catalogue indicators the data plane says a gate can read, computed by the
 * chart from these candles and the view's lead-in bars before them. A
 * catalogue name the bot already records is left out: the data plane reads it
 * as the bot's own value, so it would not be chart-computed.
 */
export function gateVariableGroups(
  view: StrategyViewResponse,
  catalogue: readonly GateCatalogueEntry[] | null,
): GateVariableGroup[] {
  const settings = Object.entries(view.settings ?? {}).flatMap(([name, value]): GateVariableChip[] =>
    typeof value === 'number' ? [{ name, hint: String(value) }] : [],
  );
  const recorded = new Set(view.declaration.values.map((value) => value.variable.toUpperCase()));
  return [
    {
      title: 'Bot’s values',
      note: null,
      chips: view.declaration.values.map((value) => ({ name: value.variable, hint: value.label })),
    },
    { title: 'Settings', note: 'as deployed', chips: settings },
    { title: 'Candle', note: null, chips: CANDLE_VARIABLES.map((name) => ({ name, hint: `the bar’s ${name}` })) },
    catalogue === null
      ? {
          title: 'Catalogue',
          note: CATALOGUE_NOTE,
          chips: [],
          unavailable: 'The data plane did not list its catalogue. Refresh to try again.',
        }
      : {
          title: 'Catalogue',
          note: CATALOGUE_NOTE,
          chips: catalogue
            .filter((indicator) => !recorded.has(indicator.variable.toUpperCase()))
            .map((indicator) => ({ name: indicator.variable, hint: indicator.description })),
        },
  ].filter((group) => group.chips.length > 0 || group.unavailable !== undefined);
}

/** A refused gate request in the data plane's words, else `fallback`. */
export function gateRefusalMessage(error: unknown, fallback: string): string {
  const message = refusalBody(error)?.['message'];
  return typeof message === 'string' && message.length > 0 ? message : fallback;
}
