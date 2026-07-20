/**
 * Render a ``StrategySpec``'s lifecycle blocks as human-readable English.
 *
 * The form builder shows these summaries above each tab so a user can
 * read what their strategy actually does without parsing JSON. The
 * functions are pure — given the same spec, they return the same
 * string — so they're trivial to unit-test.
 */

import {
  Condition,
  EntryBlock,
  ExitBlock,
  IndicatorBlock,
  LogicNode,
  Operand,
  StrategySpec,
  SurvivalRule,
} from '../../graphql/spec-strategy.models';

const OP_LABEL: Record<string, string> = {
  '<': '<',
  '<=': '≤',
  '==': '=',
  '!=': '≠',
  '>=': '≥',
  '>': '>',
};

function indicatorLabel(id: string, indicators: readonly IndicatorBlock[]): string {
  const block = indicators.find((b) => b.id === id);
  if (!block) {
    // Unknown id — happens during editing while the user adds a condition
    // before declaring the indicator. Show the id in italics-style braces
    // so the user can spot the dangling reference.
    return `{${id}}`;
  }
  return `${block.kind}(${block.period})`;
}

export function formatOperand(op: Operand, indicators: readonly IndicatorBlock[]): string {
  switch (op.kind) {
    case 'IndicatorRef':
      return indicatorLabel(op.indicator, indicators);
    case 'Const':
      return formatNumber(op.value);
    case 'Subtract':
      return `${formatOperand(op.left, indicators)} − ${formatOperand(op.right, indicators)}`;
  }
}

function formatNumber(n: number): string {
  // Compact, no trailing zeros. 0.20 → 0.2, 100 → 100, 0.005 → 0.005.
  if (Number.isInteger(n)) return n.toString();
  return n.toString();
}

export function formatCondition(
  cond: Condition | LogicNode,
  indicators: readonly IndicatorBlock[],
): string {
  // Logic groups recurse.
  if ('logic' in cond) {
    return `(${cond.conditions
      .map((c) => formatCondition(c, indicators))
      .join(cond.logic === 'AND' ? ' AND ' : ' OR ')})`;
  }

  switch (cond.kind) {
    case 'IndicatorComparison': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      return `${formatOperand(cond.left, indicators)} ${op} ${formatOperand(cond.right, indicators)}`;
    }
    case 'IndicatorBetween': {
      const inclusive = cond.inclusive !== false;
      const lo = formatNumber(cond.lo);
      const hi = formatNumber(cond.hi);
      const ind = indicatorLabel(cond.indicator, indicators);
      return inclusive
        ? `${ind} is between ${lo} and ${hi}`
        : `${ind} is strictly between ${lo} and ${hi}`;
    }
    case 'FreshCross': {
      const left = indicatorLabel(cond.left, indicators);
      const right = indicatorLabel(cond.right, indicators);
      return cond.direction === 'up'
        ? `${left} crosses above ${right}`
        : `${left} crosses below ${right}`;
    }
    case 'BarsSinceEntry': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      // "BarsSinceEntry >= 5" reads better as "5 or more bars since entry"
      // for the common case, but stay literal for other operators.
      if (cond.op === '>=') return `${cond.value} or more bars since entry`;
      if (cond.op === '>') return `more than ${cond.value} bars since entry`;
      if (cond.op === '<=') return `${cond.value} or fewer bars since entry`;
      if (cond.op === '<') return `fewer than ${cond.value} bars since entry`;
      if (cond.op === '==') return `exactly ${cond.value} bars since entry`;
      return `bars since entry ${op} ${cond.value}`;
    }
    case 'TimeOfDay': {
      const after = cond.after ?? null;
      const before = cond.before ?? null;
      if (after && before) return `between ${after} and ${before}`;
      if (after) return `after ${after}`;
      if (before) return `before ${before}`;
      return 'any time';
    }
    case 'PnLPercent': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      const pct = (cond.value * 100).toFixed(2).replace(/\.?0+$/, '');
      return `unrealized PnL ${op} ${pct}%`;
    }
    case 'PnLPoints': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      return `unrealized PnL points ${op} ${formatNumber(cond.value)}`;
    }
    case 'DrawdownFromPeak': {
      const pct = (cond.value * 100).toFixed(2).replace(/\.?0+$/, '');
      return `${pct}% retrace from peak since entry`;
    }
    case 'BarProperty': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      const propLabel: Record<string, string> = {
        range: 'bar range',
        body: 'bar body',
        range_pct: 'bar range %',
        body_pct: 'bar body %',
      };
      const label = propLabel[cond.property] ?? cond.property;
      return `${label} ${op} ${formatNumber(cond.value)}`;
    }
    case 'PredictionComparison': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      return `prediction ${cond.prediction} ${op} ${formatNumber(cond.value)}`;
    }
  }
}

export function formatEntryBlock(entry: EntryBlock, indicators: readonly IndicatorBlock[]): string {
  if (entry.conditions.length === 0) return 'Enter on every bar (no entry conditions configured).';
  const parts = entry.conditions.map((c) => formatCondition(c, indicators));
  const joiner = entry.logic === 'AND' ? ' AND ' : ' OR ';
  const sized = formatSize(entry);
  return `Enter ${sized} when ${parts.join(joiner)}.`;
}

function formatSize(entry: EntryBlock): string {
  const size = entry.size;
  if (size.kind === 'SetHoldings') {
    if (size.fraction === 1) return 'all-in';
    return `${(size.fraction * 100).toFixed(0)}% of equity`;
  }
  if (size.kind === 'FixedContracts') {
    return `${size.value} contract${size.value === 1 ? '' : 's'}`;
  }
  return '';
}

export function formatExitBlock(exit: ExitBlock, indicators: readonly IndicatorBlock[]): string {
  if (exit.conditions.length === 0) return 'No signal-flip exit configured.';
  const parts = exit.conditions.map((c) => formatCondition(c, indicators));
  const joiner = exit.logic === 'AND' ? ' AND ' : ' OR ';
  return `Exit when ${parts.join(joiner)}.`;
}

export function formatSurvivalRule(
  rule: SurvivalRule,
  indicators: readonly IndicatorBlock[],
): string {
  const parts = rule.when.conditions.map((c) => formatCondition(c, indicators));
  const joiner = rule.when.logic === 'AND' ? ' AND ' : ' OR ';
  const action = rule.action.kind === 'CLOSE_ALL' ? 'close the position' : rule.action.kind;
  return `${rule.name}: when ${parts.join(joiner)}, ${action}.`;
}

export function formatSurvivalBlock(
  rules: readonly SurvivalRule[],
  indicators: readonly IndicatorBlock[],
): string {
  if (rules.length === 0) return 'No manage rules configured.';
  return rules.map((r) => '• ' + formatSurvivalRule(r, indicators)).join('\n');
}

/**
 * Top-of-page one-paragraph summary covering all three lifecycle
 * blocks. Used in the page header so a user lands on a familiar
 * strategy and can read what it does without scrolling.
 */
export function formatStrategySummary(spec: StrategySpec): string {
  const parts: string[] = [];
  parts.push(formatEntryBlock(spec.entry, spec.indicators));
  if (spec.survival && spec.survival.length > 0) {
    parts.push(`Manage rules: ${spec.survival.map((r) => r.name).join(', ')}.`);
  }
  parts.push(formatExitBlock(spec.exit, spec.indicators));
  return parts.join(' ');
}

// ---------------------------------------------------------------------------
// Structured summary fragments — for rendering the rich Strategy Summary
// hero with colored chips for indicators / numbers / verbs / bull / bear.
// The component template walks the array via @switch on .kind.
// ---------------------------------------------------------------------------
export type SummaryFragmentKind =
  | 'text'
  | 'strong'
  | 'verb'
  | 'ind'
  | 'num'
  | 'muted'
  | 'bull'
  | 'bear'
  | 'warn';

export interface SummaryFragment {
  readonly kind: SummaryFragmentKind;
  readonly text: string;
}

function f(kind: SummaryFragmentKind, text: string): SummaryFragment {
  return { kind, text };
}

function indFragment(id: string, indicators: readonly IndicatorBlock[]): SummaryFragment {
  const block = indicators.find((b) => b.id === id);
  if (!block) return f('bear', `{${id}}`);
  return f('ind', `${block.kind}(${block.period})`);
}

function operandFragments(op: Operand, indicators: readonly IndicatorBlock[]): SummaryFragment[] {
  switch (op.kind) {
    case 'IndicatorRef':
      return [indFragment(op.indicator, indicators)];
    case 'Const':
      return [f('num', formatNumberLocal(op.value))];
    case 'Subtract':
      return [
        ...operandFragments(op.left, indicators),
        f('text', ' '),
        f('verb', '−'),
        f('text', ' '),
        ...operandFragments(op.right, indicators),
      ];
  }
}

function formatNumberLocal(n: number): string {
  if (Number.isInteger(n)) return n.toString();
  return n.toString();
}

function condFragments(cond: Condition, indicators: readonly IndicatorBlock[]): SummaryFragment[] {
  // Reuse the OP_LABEL map declared at the top of this module.
  switch (cond.kind) {
    case 'FreshCross': {
      const verb = cond.direction === 'up' ? 'crosses above' : 'crosses below';
      return [
        indFragment(cond.left, indicators),
        f('text', ' '),
        f('verb', verb),
        f('text', ' '),
        indFragment(cond.right, indicators),
      ];
    }
    case 'IndicatorComparison': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      return [
        ...operandFragments(cond.left, indicators),
        f('text', ' '),
        f('verb', op),
        f('text', ' '),
        ...operandFragments(cond.right, indicators),
      ];
    }
    case 'IndicatorBetween':
      return [
        indFragment(cond.indicator, indicators),
        f('text', ' '),
        f('verb', 'is between'),
        f('text', ' '),
        f('num', formatNumberLocal(cond.lo)),
        f('text', ' '),
        f('muted', 'and'),
        f('text', ' '),
        f('num', formatNumberLocal(cond.hi)),
      ];
    case 'TimeOfDay': {
      if (cond.after && cond.before) {
        return [
          f('verb', 'between'),
          f('text', ' '),
          f('num', cond.after),
          f('text', ' '),
          f('muted', 'and'),
          f('text', ' '),
          f('num', cond.before),
          f('text', ' '),
          f('muted', 'ET'),
        ];
      }
      if (cond.after) {
        return [f('verb', 'after'), f('text', ' '), f('num', cond.after), f('text', ' '), f('muted', 'ET')];
      }
      if (cond.before) {
        return [f('verb', 'before'), f('text', ' '), f('num', cond.before), f('text', ' '), f('muted', 'ET')];
      }
      return [f('muted', 'any time')];
    }
    case 'BarsSinceEntry': {
      const v = cond.value;
      if (cond.op === '>=') return [f('num', String(v)), f('text', ' '), f('verb', 'or more bars since entry')];
      if (cond.op === '>') return [f('verb', 'more than'), f('text', ' '), f('num', String(v)), f('text', ' '), f('muted', 'bars since entry')];
      if (cond.op === '<=') return [f('num', String(v)), f('text', ' '), f('verb', 'or fewer bars since entry')];
      if (cond.op === '<') return [f('verb', 'fewer than'), f('text', ' '), f('num', String(v)), f('text', ' '), f('muted', 'bars since entry')];
      if (cond.op === '==') return [f('verb', 'exactly'), f('text', ' '), f('num', String(v)), f('text', ' '), f('muted', 'bars since entry')];
      const op = OP_LABEL[cond.op] ?? cond.op;
      return [f('muted', 'bars since entry'), f('text', ' '), f('verb', op), f('text', ' '), f('num', String(v))];
    }
    case 'PnLPercent': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      const pct = (cond.value * 100).toFixed(2).replace(/\.?0+$/, '');
      const cls: SummaryFragmentKind = cond.op.includes('>') ? 'bull' : 'bear';
      return [
        f('verb', 'unrealized P&L'),
        f('text', ' '),
        f('verb', op),
        f('text', ' '),
        f(cls, `${pct}%`),
      ];
    }
    case 'PnLPoints': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      return [
        f('verb', 'unrealized P&L points'),
        f('text', ' '),
        f('verb', op),
        f('text', ' '),
        f('num', formatNumberLocal(cond.value)),
      ];
    }
    case 'DrawdownFromPeak': {
      const pct = (cond.value * 100).toFixed(2).replace(/\.?0+$/, '');
      return [f('bear', `${pct}%`), f('text', ' '), f('verb', 'retrace from peak since entry')];
    }
    case 'BarProperty': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      const propLabel: Record<string, string> = {
        range: 'bar range',
        body: 'bar body',
        range_pct: 'bar range %',
        body_pct: 'bar body %',
      };
      return [
        f('verb', propLabel[cond.property] ?? cond.property),
        f('text', ' '),
        f('verb', op),
        f('text', ' '),
        f('num', formatNumberLocal(cond.value)),
      ];
    }
    case 'PredictionComparison': {
      const op = OP_LABEL[cond.op] ?? cond.op;
      return [
        f('verb', `prediction ${cond.prediction}`),
        f('text', ' '),
        f('verb', op),
        f('text', ' '),
        f('num', formatNumberLocal(cond.value)),
      ];
    }
  }
}

function joinedConditionFragments(
  conditions: readonly (Condition | LogicNode)[],
  logic: 'AND' | 'OR',
  indicators: readonly IndicatorBlock[],
): SummaryFragment[] {
  const out: SummaryFragment[] = [];
  const joiner = logic === 'OR' ? ' OR ' : ' AND ';
  conditions.forEach((c, i) => {
    if (i > 0) out.push(f('muted', joiner));
    if ('logic' in c) {
      // Nested logic group — bracket it.
      out.push(f('text', '('));
      const inner = c as LogicNode;
      out.push(...joinedConditionFragments(
        inner.conditions,
        inner.logic,
        indicators,
      ));
      out.push(f('text', ')'));
    } else {
      out.push(...condFragments(c as Condition, indicators));
    }
  });
  return out;
}

/**
 * Build the rich-fragment Strategy Summary used by the design's hero card.
 * Returns one line per lifecycle block, with chips for indicators / numbers /
 * verbs / bull / bear. The component template walks the fragments via
 * @switch on .kind to pick the right CSS class.
 */
export function buildSummaryFragments(spec: StrategySpec): SummaryFragment[] {
  const out: SummaryFragment[] = [];
  const inds = spec.indicators;
  const entry = spec.entry;
  const survival = spec.survival ?? [];
  const exit = spec.exit;
  const hasEntry = entry && entry.conditions.length > 0;
  const hasExit = exit && exit.conditions.length > 0;

  // ── Entry ────────────────────────────────────────────────────────
  out.push(f('strong', 'Entry —'));
  out.push(f('text', ' '));
  if (hasEntry) {
    const size = entry.size;
    if (size.kind === 'SetHoldings') {
      if (size.fraction >= 0.999) out.push(f('verb', 'go all-in'));
      else {
        out.push(f('verb', 'size to'));
        out.push(f('text', ' '));
        out.push(f('num', `${(size.fraction * 100).toFixed(0)}%`));
        out.push(f('text', ' '));
        out.push(f('muted', 'of equity'));
      }
    } else if (size.kind === 'FixedContracts') {
      out.push(f('verb', 'buy'));
      out.push(f('text', ' '));
      out.push(f('num', String(size.value)));
      out.push(f('text', ' '));
      out.push(f('muted', 'contracts'));
    }
    out.push(f('text', ' '));
    out.push(f('muted', 'when'));
    out.push(f('text', ' '));
    out.push(...joinedConditionFragments(entry.conditions, entry.logic, inds));
    out.push(f('text', '.'));
  } else {
    out.push(
      f('muted', 'no entry conditions yet — add a Crossover signal or Indicator comparison to start.'),
    );
  }

  // ── Manage ───────────────────────────────────────────────────────
  if (survival.length > 0) {
    out.push(f('text', ' '));
    out.push(f('strong', 'Manage —'));
    out.push(f('text', ' '));
    survival.forEach((r, i) => {
      if (i > 0) out.push(f('muted', '; '));
      out.push(f('verb', r.name));
      out.push(f('text', ' '));
      out.push(f('muted', 'when'));
      out.push(f('text', ' '));
      out.push(...joinedConditionFragments(r.when.conditions, r.when.logic, inds));
      out.push(f('text', ' '));
      out.push(f('muted', '→'));
      out.push(f('text', ' '));
      out.push(f('verb', 'close'));
      out.push(f('text', '.'));
    });
  }

  // ── Exit ─────────────────────────────────────────────────────────
  out.push(f('text', ' '));
  out.push(f('strong', 'Exit —'));
  out.push(f('text', ' '));
  if (hasExit) {
    out.push(f('muted', 'close when'));
    out.push(f('text', ' '));
    out.push(...joinedConditionFragments(exit.conditions, exit.logic, inds));
    out.push(f('text', '.'));
  } else if (survival.length === 0) {
    out.push(f('warn', 'no exit configured — strategy will hold until the run window ends.'));
  } else {
    out.push(f('muted', 'close handled by manage rules above.'));
  }
  return out;
}

/**
 * One-line plain-English of a single condition. Used inside condition
 * cards in read mode. Returns a string (no rich coloring) — the card
 * itself shows the colored category tag separately.
 */
export function formatConditionLine(
  cond: Condition | { logic: 'AND' | 'OR' },
  indicators: readonly IndicatorBlock[],
): string {
  return formatCondition(cond as Condition, indicators);
}
