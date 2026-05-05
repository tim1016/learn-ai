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
} from '../../graphql/spec-strategy-types';

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
    case 'BarField':
      return `bar.${op.field}`;
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
