/**
 * Discoverable snippet catalog for the runner UI.
 *
 * The Pydantic schema in PythonDataService is the source of truth for
 * shape; these snippets are hand-curated examples that match the
 * schema and pair with the evaluator's Phase-1/2.x capability surface.
 * If the schema gains a new condition kind, add it here in the same PR.
 *
 * The snippet's ``example`` field is a JSON value (object), not a
 * string — the runner UI stringifies on copy/insert. Keeping it as a
 * value lets the inserter splice it into the correct array via
 * ``JSON.parse``-edit-``JSON.stringify`` without text-level fiddling.
 */

export interface SpecSnippet {
  /** Stable id for the catalog. */
  readonly id: string;
  /** Short human label shown in the catalog list. */
  readonly label: string;
  /** One-line description of what the snippet does. */
  readonly description: string;
  /** Where in the spec this snippet belongs — drives the Insert button. */
  readonly target: 'indicators' | 'entry.conditions' | 'exit.conditions' | 'survival';
  /** Example JSON value for the snippet. */
  readonly example: object;
}

// ---------------------------------------------------------------------------
// Indicator kinds.
// ---------------------------------------------------------------------------
const INDICATORS: readonly SpecSnippet[] = [
  {
    id: 'ind.sma',
    label: 'SMA — Simple Moving Average',
    description: 'Window length set by `period`. Source defaults to close.',
    target: 'indicators',
    example: { id: 'sma_20', kind: 'SMA', period: 20, source: 'close' },
  },
  {
    id: 'ind.ema',
    label: 'EMA — Exponential Moving Average',
    description: 'SMA-seeded then standard EMA recursion. LEAN-parity.',
    target: 'indicators',
    example: { id: 'ema_20', kind: 'EMA', period: 20, source: 'close' },
  },
  {
    id: 'ind.rsi',
    label: 'RSI — Wilders Relative Strength Index',
    description: 'Wilders smoothing only; engine RSI does not support simple smoothing.',
    target: 'indicators',
    example: {
      id: 'rsi_14',
      kind: 'RSI',
      period: 14,
      source: 'close',
      ma_type: 'wilders',
    },
  },
  {
    id: 'ind.adx',
    label: 'ADX — Average Directional Index',
    description: 'Wilder DMI/ADX. Consumes full OHLC bars; warmup = 2 × period.',
    target: 'indicators',
    example: { id: 'adx_14', kind: 'ADX', period: 14 },
  },
  {
    id: 'ind.macd',
    label: 'MACD — Moving Average Convergence Divergence',
    description:
      '`period` carries slow_period; fast and signal default to 12 and 9. ' +
      '`current_value` is the MACD line.',
    target: 'indicators',
    example: {
      id: 'macd',
      kind: 'MACD',
      period: 26,
      fast_period: 12,
      signal_period: 9,
      source: 'close',
    },
  },
  {
    id: 'ind.supertrend',
    label: 'SUPERTREND — ATR-band Supertrend',
    description:
      '`period` is the ATR window; `multiplier` defaults to 3.0. ' +
      'Consumes full OHLC bars.',
    target: 'indicators',
    example: {
      id: 'st_10_3',
      kind: 'SUPERTREND',
      period: 10,
      multiplier: 3.0,
    },
  },
];

// ---------------------------------------------------------------------------
// Condition kinds — usable in entry, exit, or survival.when blocks.
// `target` defaults to entry.conditions in the catalog UI, but Insert
// also offers exit.conditions for any condition that gates an open
// position (PnL/Drawdown/BarsSinceEntry). The component decides.
// ---------------------------------------------------------------------------
const CONDITIONS: readonly SpecSnippet[] = [
  {
    id: 'cond.ind_compare',
    label: 'IndicatorComparison — compare two operands',
    description:
      'Operands can be IndicatorRef, Const, or Subtract. Catches RSI > 70, ' +
      'EMA gap >= 0.20, MACD > 0, etc.',
    target: 'entry.conditions',
    example: {
      kind: 'IndicatorComparison',
      left: { kind: 'IndicatorRef', indicator: 'rsi_14' },
      op: '>',
      right: { kind: 'Const', value: 70 },
    },
  },
  {
    id: 'cond.ind_between',
    label: 'IndicatorBetween — value within range',
    description: 'Inclusive by default. RSI in [50, 70] etc.',
    target: 'entry.conditions',
    example: { kind: 'IndicatorBetween', indicator: 'rsi_14', lo: 50, hi: 70, inclusive: true },
  },
  {
    id: 'cond.fresh_cross',
    label: 'FreshCross — fresh crossover of two indicators',
    description:
      'Fires only on the bar where the sign flips. Seeded without firing on ' +
      'the first eligible bar (same as SmaCrossoverAlgorithm).',
    target: 'entry.conditions',
    example: { kind: 'FreshCross', left: 'ema_5', right: 'ema_10', direction: 'up' },
  },
  {
    id: 'cond.bars_since_entry',
    label: 'BarsSinceEntry — bars since the entry fired',
    description:
      'Entry bar is 0; next bar is 1; etc. Use in exit.conditions for ' +
      'fixed-bar exits (5-bar hold, etc.).',
    target: 'exit.conditions',
    example: { kind: 'BarsSinceEntry', op: '>=', value: 5 },
  },
  {
    id: 'cond.time_of_day',
    label: 'TimeOfDay — restrict by wall-clock window',
    description:
      'Entry only inside RTH, etc. Format HH:MM. tz defaults to America/New_York.',
    target: 'entry.conditions',
    example: { kind: 'TimeOfDay', after: '09:45', before: '15:30', tz: 'America/New_York' },
  },
  {
    id: 'cond.pnl_pct',
    label: 'PnLPercent — unrealized PnL as fraction',
    description:
      'For survival rules. Value is a fraction, not percent: -0.01 means -1%.',
    target: 'survival',
    example: { kind: 'PnLPercent', op: '<=', value: -0.01 },
  },
  {
    id: 'cond.pnl_pts',
    label: 'PnLPoints — unrealized PnL in price points',
    description: 'Same gating as PnLPercent; absolute units instead of fraction.',
    target: 'survival',
    example: { kind: 'PnLPoints', op: '<=', value: -1.5 },
  },
  {
    id: 'cond.drawdown_from_peak',
    label: 'DrawdownFromPeak — trailing-stop primitive',
    description:
      'Tracks peak-since-entry; fires when current close has retraced by ' +
      '>= value. Resets between trades.',
    target: 'survival',
    example: { kind: 'DrawdownFromPeak', value: 0.005 },
  },
  {
    id: 'cond.bar_property',
    label: 'BarProperty — bar-shape filter',
    description:
      'Compares range / body / range_pct / body_pct to a threshold. ' +
      'Useful for ORB-style minimum-range entry filters.',
    target: 'entry.conditions',
    example: { kind: 'BarProperty', property: 'range_pct', op: '>=', value: 0.003 },
  },
];

// ---------------------------------------------------------------------------
// Survival rules — full rule shape with name + when + action wrapper.
// ---------------------------------------------------------------------------
const SURVIVAL: readonly SpecSnippet[] = [
  {
    id: 'surv.stop_loss',
    label: 'Stop loss — close on -1% drawdown',
    description: 'PnLPercent <= -0.01 → CLOSE_ALL. First-match-wins ordering.',
    target: 'survival',
    example: {
      name: 'stop loss',
      when: {
        logic: 'AND',
        conditions: [{ kind: 'PnLPercent', op: '<=', value: -0.01 }],
      },
      action: { kind: 'CLOSE_ALL' },
    },
  },
  {
    id: 'surv.profit_target',
    label: 'Profit target — close on +0.5% gain',
    description: 'PnLPercent >= 0.005 → CLOSE_ALL.',
    target: 'survival',
    example: {
      name: 'profit target',
      when: {
        logic: 'AND',
        conditions: [{ kind: 'PnLPercent', op: '>=', value: 0.005 }],
      },
      action: { kind: 'CLOSE_ALL' },
    },
  },
  {
    id: 'surv.trailing_stop',
    label: 'Trailing stop — close on 1% retrace from peak',
    description: 'DrawdownFromPeak 0.01 → CLOSE_ALL.',
    target: 'survival',
    example: {
      name: 'trailing stop 1%',
      when: {
        logic: 'AND',
        conditions: [{ kind: 'DrawdownFromPeak', value: 0.01 }],
      },
      action: { kind: 'CLOSE_ALL' },
    },
  },
];

export interface SnippetGroup {
  readonly title: string;
  readonly snippets: readonly SpecSnippet[];
}

export const SNIPPET_GROUPS: readonly SnippetGroup[] = [
  { title: 'Indicators', snippets: INDICATORS },
  { title: 'Conditions', snippets: CONDITIONS },
  { title: 'Survival rules (Manage)', snippets: SURVIVAL },
];

/**
 * Insert a snippet into the spec JSON at the appropriate array.
 *
 * Returns the new JSON string, or throws if the input string is not a
 * valid JSON object or doesn't have the expected shape (indicators[],
 * entry.conditions[], etc.).
 *
 * The inserter is dumb on purpose — it doesn't deduplicate, doesn't
 * rewrite ids, doesn't validate against the Pydantic schema. The user
 * is expected to inspect the result and tweak. Validation happens at
 * Run time when the spec is round-tripped through the backend.
 */
export function insertSnippet(specJson: string, snippet: SpecSnippet): string {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(specJson) as Record<string, unknown>;
  } catch (e) {
    throw new Error(
      `Cannot insert snippet — spec JSON is invalid: ${e instanceof Error ? e.message : String(e)}`,
      { cause: e },
    );
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('Cannot insert snippet — spec JSON must be an object');
  }

  // Clone the snippet example so the catalog's own copy stays pristine.
  const example = JSON.parse(JSON.stringify(snippet.example)) as object;

  switch (snippet.target) {
    case 'indicators': {
      const arr = Array.isArray(parsed['indicators']) ? (parsed['indicators'] as object[]) : [];
      arr.push(example);
      parsed['indicators'] = arr;
      break;
    }
    case 'entry.conditions': {
      const entry = (parsed['entry'] ?? {}) as { conditions?: object[] };
      const arr = Array.isArray(entry.conditions) ? entry.conditions : [];
      arr.push(example);
      entry.conditions = arr;
      parsed['entry'] = entry;
      break;
    }
    case 'exit.conditions': {
      const exit = (parsed['exit'] ?? {}) as { conditions?: object[] };
      const arr = Array.isArray(exit.conditions) ? exit.conditions : [];
      arr.push(example);
      exit.conditions = arr;
      parsed['exit'] = exit;
      break;
    }
    case 'survival': {
      const arr = Array.isArray(parsed['survival']) ? (parsed['survival'] as object[]) : [];
      arr.push(example);
      parsed['survival'] = arr;
      break;
    }
  }

  return JSON.stringify(parsed, null, 2);
}
