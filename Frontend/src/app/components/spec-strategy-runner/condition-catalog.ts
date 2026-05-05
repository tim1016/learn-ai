/**
 * Trader-facing catalog metadata for condition kinds.
 *
 * Each entry maps a raw spec ``kind`` to:
 *   * a friendly ``label`` (replaces the raw "IndicatorComparison" with
 *     "Indicator comparison" etc.)
 *   * a ``group`` bucket (signals / filters / risk / timing / shape)
 *     used to color-code chips and group the "Add condition" picker
 *   * a ``blurb`` for the hover tooltip on the picker
 *   * an ``example`` shown under the blurb
 *
 * Mirrors ``project/strategy_spec_redesign/catalog.jsx`` from the
 * design bundle. The Pydantic schema is the source of truth for shape;
 * this file is purely UI metadata.
 */

export type ConditionGroup = 'signal' | 'filter' | 'risk' | 'timing' | 'shape';

export interface ConditionGroupMeta {
  readonly label: string;
  readonly hint: string;
}

export const CONDITION_GROUPS: Record<ConditionGroup, ConditionGroupMeta> = {
  signal: {
    label: 'Signals',
    hint: 'Triggers that fire on a price/indicator event',
  },
  filter: {
    label: 'Filters',
    hint: 'Gating conditions — only fire when an indicator is in a range',
  },
  risk: {
    label: 'Risk & exits',
    hint: 'Profit, loss, drawdown thresholds',
  },
  timing: {
    label: 'Timing',
    hint: 'When in the trade or session a rule may fire',
  },
  shape: {
    label: 'Bar shape',
    hint: 'Properties of the candle itself',
  },
};

export interface ConditionMeta {
  readonly label: string;
  readonly short: string;
  readonly group: ConditionGroup;
  readonly blurb: string;
  readonly example: string;
}

export type ConditionKind =
  | 'IndicatorComparison'
  | 'IndicatorBetween'
  | 'FreshCross'
  | 'TimeOfDay'
  | 'BarsSinceEntry'
  | 'PnLPercent'
  | 'PnLPoints'
  | 'DrawdownFromPeak'
  | 'BarProperty';

export const CONDITION_CATALOG: Record<ConditionKind, ConditionMeta> = {
  FreshCross: {
    label: 'Crossover signal',
    short: 'Crossover',
    group: 'signal',
    blurb:
      'Fires the moment a fast line moves above (or below) a slow line. ' +
      'The classic trend-change trigger.',
    example: 'EMA(5) crosses above EMA(10)',
  },
  IndicatorComparison: {
    label: 'Indicator comparison',
    short: 'Compare',
    group: 'signal',
    blurb:
      'Compare two indicators (or an indicator and a number). The condition ' +
      'holds while the comparison is true — not just on the bar it became true.',
    example: 'EMA(5) − EMA(10) ≥ 0.20',
  },
  IndicatorBetween: {
    label: 'Indicator in range',
    short: 'In range',
    group: 'filter',
    blurb:
      'Holds while an indicator sits between two values. Common gate for ' +
      'RSI bands or volatility regimes.',
    example: 'RSI(14) between 50 and 70',
  },
  TimeOfDay: {
    label: 'Time window',
    short: 'Time',
    group: 'timing',
    blurb:
      'Restrict the rule to a window inside the trading session. Times ' +
      'are interpreted in America/New_York.',
    example: 'Between 09:45 and 15:30',
  },
  BarsSinceEntry: {
    label: 'Hold time',
    short: 'Hold',
    group: 'timing',
    blurb:
      'How many bars we have been in the trade. Use it to time-out the ' +
      'position or to delay an exit until the trade has had a chance to work.',
    example: '5 or more bars since entry',
  },
  PnLPercent: {
    label: 'Profit / loss target',
    short: 'P&L %',
    group: 'risk',
    blurb:
      'Open-trade unrealized return as a percentage of entry price. Use ' +
      'positive values for take-profits, negatives for stops.',
    example: 'unrealized P&L ≤ −1.5%',
  },
  PnLPoints: {
    label: 'P&L points',
    short: 'P&L pts',
    group: 'risk',
    blurb:
      'Unrealized P&L in raw price points. Useful when stop sizes are ' +
      'dollar-defined rather than percentage-defined.',
    example: 'unrealized P&L points ≥ 2.0',
  },
  DrawdownFromPeak: {
    label: 'Trailing drawdown',
    short: 'Trail',
    group: 'risk',
    blurb:
      'How far the unrealized P&L has retraced from its in-trade peak. The ' +
      'standard trailing-stop primitive.',
    example: '50% retrace from peak since entry',
  },
  BarProperty: {
    label: 'Bar shape',
    short: 'Shape',
    group: 'shape',
    blurb:
      'Compare a property of the current candle (range, body, range %, ' +
      'body %) against a number.',
    example: 'bar body ≥ 1.0',
  },
};

/**
 * Filter the catalog to the kinds usable in a given lifecycle context.
 *
 *   * entry → no trade-only kinds (BarsSinceEntry / PnLPercent / PnLPoints
 *     / DrawdownFromPeak only make sense once a position is open).
 *   * exit / manage → all kinds usable.
 */
export function conditionsForContext(
  ctx: 'entry' | 'exit' | 'manage',
): ReadonlyArray<ConditionKind> {
  const tradeOnly: ReadonlyArray<ConditionKind> = [
    'BarsSinceEntry',
    'PnLPercent',
    'PnLPoints',
    'DrawdownFromPeak',
  ];
  const all = Object.keys(CONDITION_CATALOG) as ConditionKind[];
  if (ctx === 'entry') return all.filter((k) => !tradeOnly.includes(k));
  return all;
}

/**
 * Group condition kinds by ``group`` for the picker UI.
 * Returns one entry per non-empty group, preserving CONDITION_GROUPS' order.
 */
export function groupedConditionsForContext(
  ctx: 'entry' | 'exit' | 'manage',
): ReadonlyArray<{ group: ConditionGroup; meta: ConditionGroupMeta; kinds: ReadonlyArray<ConditionKind> }> {
  const allowed = conditionsForContext(ctx);
  const groups = Object.keys(CONDITION_GROUPS) as ConditionGroup[];
  const out: { group: ConditionGroup; meta: ConditionGroupMeta; kinds: ConditionKind[] }[] = [];
  for (const g of groups) {
    const kinds = allowed.filter((k) => CONDITION_CATALOG[k].group === g);
    if (kinds.length > 0) {
      out.push({ group: g, meta: CONDITION_GROUPS[g], kinds });
    }
  }
  return out;
}
