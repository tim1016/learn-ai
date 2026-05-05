/**
 * Strategy spec + run config validation.
 *
 * Ports ``project/strategy_spec_redesign/validation.jsx`` to TypeScript.
 * Pure function — given the spec and run config, returns a list of
 * issues with severity, text, hint, and optional one-shot fix label.
 *
 * Severities:
 *   * ``error`` — blocks Run.
 *   * ``warn``  — surfaces an issue but the run is allowed to proceed.
 *   * ``info``  — best-practice nudge; never blocks.
 */

import {
  Condition,
  Operand,
  StrategySpec,
} from '../../graphql/spec-strategy-types';

export type IssueSeverity = 'error' | 'warn' | 'info';

export interface ValidationIssue {
  readonly sev: IssueSeverity;
  readonly text: string;
  readonly hint?: string;
  readonly fix?: string;
}

export interface RunConfig {
  readonly start?: string;
  readonly end?: string;
  readonly initialCash?: number;
  readonly fillMode?: string;
  readonly resolutionMinutes?: number;
}

export interface DataAvailability {
  readonly symbol: string;
  readonly earliest: string;
  readonly latest: string;
}

export function validateStrategy(
  spec: StrategySpec,
  runCfg: RunConfig | null = null,
  dataAvailability: DataAvailability | null = null,
): readonly ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const inds = spec.indicators ?? [];
  const indIds = new Set(inds.map((i) => i.id));

  // ── Hard errors ────────────────────────────────────────────────────
  if (inds.length === 0) {
    issues.push({
      sev: 'error',
      text: 'No indicators defined.',
      hint: 'Add at least one indicator (e.g. EMA-9) so your conditions have something to reference.',
      fix: 'Add EMA-9',
    });
  }

  inds.forEach((ind, i) => {
    if (!ind.id || ind.id.trim() === '') {
      issues.push({
        sev: 'error',
        text: `Indicator #${i + 1} has no id.`,
        hint: 'Give each indicator a short, unique id like ema9 or rsi14.',
      });
    }
  });

  // Duplicate ids
  const seen = new Set<string>();
  inds.forEach((ind) => {
    const id = (ind.id ?? '').trim();
    if (!id) return;
    if (seen.has(id)) {
      issues.push({
        sev: 'error',
        text: `Two indicators share the id "${id}".`,
        hint: 'Rename one of them — every reference resolves to the first match.',
      });
    } else {
      seen.add(id);
    }
  });

  // Dangling refs
  function checkOperand(op: Operand, where: string): void {
    if (!op) return;
    if (op.kind === 'IndicatorRef' && !indIds.has(op.indicator)) {
      issues.push({
        sev: 'error',
        text: `${where} references unknown indicator "${op.indicator}".`,
        hint: 'Either add the indicator to your reference library or pick an existing one.',
      });
    }
    if (op.kind === 'Subtract') {
      checkOperand(op.left, where);
      checkOperand(op.right, where);
    }
  }

  function checkCond(c: Condition, where: string): void {
    switch (c.kind) {
      case 'IndicatorComparison':
        checkOperand(c.left, where);
        checkOperand(c.right, where);
        break;
      case 'IndicatorBetween':
        if (!indIds.has(c.indicator)) {
          issues.push({
            sev: 'error',
            text: `${where} references unknown indicator "${c.indicator}".`,
            hint: 'Pick an indicator from your reference library.',
          });
        }
        if (c.lo > c.hi) {
          issues.push({
            sev: 'error',
            text: `${where} range is reversed (lo=${c.lo} > hi=${c.hi}).`,
            hint: 'Swap the values so lo ≤ hi.',
          });
        }
        break;
      case 'FreshCross':
        if (!indIds.has(c.left)) {
          issues.push({
            sev: 'error',
            text: `${where} crossover left side references unknown "${c.left}".`,
          });
        }
        if (!indIds.has(c.right)) {
          issues.push({
            sev: 'error',
            text: `${where} crossover right side references unknown "${c.right}".`,
          });
        }
        if (c.left && c.right && c.left === c.right) {
          issues.push({
            sev: 'warn',
            text: `${where} crosses an indicator with itself — this never fires.`,
            hint: 'Pick two different indicators on each side of the cross.',
          });
        }
        break;
      default:
        break;
    }
  }

  (spec.entry?.conditions ?? []).forEach((c, i) =>
    checkCond(c as Condition, `Entry condition #${i + 1}`),
  );
  (spec.exit?.conditions ?? []).forEach((c, i) =>
    checkCond(c as Condition, `Exit condition #${i + 1}`),
  );
  (spec.survival ?? []).forEach((r) =>
    (r.when?.conditions ?? []).forEach((c, i) =>
      checkCond(c as Condition, `Manage rule "${r.name}" condition #${i + 1}`),
    ),
  );

  if (!spec.entry || (spec.entry.conditions ?? []).length === 0) {
    issues.push({
      sev: 'error',
      text: 'Entry block has no conditions.',
      hint: 'A strategy needs at least one entry condition or it will never open a trade.',
    });
  }

  // ── Range / sanity ────────────────────────────────────────────────
  inds.forEach((ind) => {
    if (ind.period != null && ind.period < 5) {
      issues.push({
        sev: 'warn',
        text: `${ind.id} has period ${ind.period} — very short.`,
        hint: 'Periods below 5 bars are often noise. Consider 9 or 14 unless you specifically want a fast-reacting line.',
      });
    }
    if (
      ind.kind === 'MACD' &&
      ind.fast_period != null &&
      ind.fast_period >= ind.period
    ) {
      issues.push({
        sev: 'error',
        text: `${ind.id}: fast period (${ind.fast_period}) must be smaller than slow period (${ind.period}).`,
        hint: 'A MACD where fast ≥ slow produces a flat line.',
      });
    }
  });

  // Position size
  const sizeBlock = spec.entry?.size;
  if (sizeBlock?.kind === 'SetHoldings') {
    const sf = sizeBlock.fraction;
    if (sf < 0 || sf > 1.0001) {
      issues.push({
        sev: 'error',
        text: `Position size is ${(sf * 100).toFixed(0)}% of equity — must be 0–100%.`,
        hint: 'Margin and leverage live in the broker config, not here.',
      });
    }
  }

  // RSI bounds
  [...(spec.entry?.conditions ?? []), ...(spec.exit?.conditions ?? [])].forEach((c) => {
    const cond = c as Condition;
    if (cond.kind === 'IndicatorBetween') {
      const ind = inds.find((b) => b.id === cond.indicator);
      if (ind && ind.kind === 'RSI' && (cond.lo < 0 || cond.hi > 100)) {
        issues.push({
          sev: 'warn',
          text: `RSI range ${cond.lo}–${cond.hi} extends beyond 0–100.`,
          hint: 'RSI is bounded 0–100. Anything outside that range will never trigger.',
        });
      }
    }
  });

  // EMA fast/slow ordering
  (spec.entry?.conditions ?? []).forEach((c) => {
    const cond = c as Condition;
    if (cond.kind === 'FreshCross') {
      const a = inds.find((b) => b.id === cond.left);
      const b = inds.find((b) => b.id === cond.right);
      if (
        a &&
        b &&
        a.kind === 'EMA' &&
        b.kind === 'EMA' &&
        a.period >= b.period
      ) {
        issues.push({
          sev: 'warn',
          text: `Crossover uses ${a.kind}(${a.period}) over ${b.kind}(${b.period}) — the "fast" line isn't faster.`,
          hint: 'For a bullish trend trigger, the fast EMA should have the smaller period.',
        });
      }
    }
  });

  // ── Realism warnings ──────────────────────────────────────────────
  const noExit = (spec.exit?.conditions ?? []).length === 0;
  const noManage = (spec.survival ?? []).length === 0;
  if (noExit && noManage) {
    issues.push({
      sev: 'warn',
      text:
        'No exit conditions or manage rules — the strategy never sells until the run window ends.',
      hint:
        'Add an exit signal (e.g. opposite crossover) or a manage rule (e.g. trailing stop).',
      fix: 'Add a 1.5% stop-loss',
    });
  } else if (noExit && !noManage) {
    issues.push({
      sev: 'info',
      text: 'No signal-flip exit. Manage rules will handle position closure.',
    });
  }

  // ── Best-practice nudges ──────────────────────────────────────────
  const hasIntradayCondition = (spec.entry?.conditions ?? []).some(
    (c) => (c as Condition).kind === 'TimeOfDay',
  );
  if (
    !hasIntradayCondition &&
    runCfg?.resolutionMinutes != null &&
    runCfg.resolutionMinutes < 60
  ) {
    issues.push({
      sev: 'info',
      text: 'Intraday strategy with no time-of-day filter.',
      hint:
        'Most intraday strategies skip the open auction (first 15 min) and avoid the close. Consider a Time window from 09:45 to 15:30 ET.',
    });
  }

  // Unused indicators
  const refIds = collectIndicatorReferences(spec);
  inds.forEach((ind) => {
    if (!refIds.has(ind.id)) {
      issues.push({
        sev: 'info',
        text: `${ind.id} is defined but never referenced.`,
        hint: 'Either remove it or use it in a condition.',
      });
    }
  });

  // ── Run config ────────────────────────────────────────────────────
  if (runCfg) {
    if (runCfg.start && runCfg.end && runCfg.start > runCfg.end) {
      issues.push({
        sev: 'error',
        text: 'End date is before start date.',
        hint: 'Swap the dates or pick a later end.',
      });
    }
    if (dataAvailability && runCfg.start && runCfg.end) {
      if (runCfg.start < dataAvailability.earliest) {
        issues.push({
          sev: 'warn',
          text: `Start date is before available data (${dataAvailability.earliest}).`,
          hint: `${dataAvailability.symbol} only has data back to ${dataAvailability.earliest}. The run will silently truncate.`,
        });
      }
      if (runCfg.end > dataAvailability.latest) {
        issues.push({
          sev: 'warn',
          text: `End date is after available data (${dataAvailability.latest}).`,
          hint: 'Pick an earlier end date or fetch more history from Data Lab.',
        });
      }
    }
  }

  return issues;
}

/**
 * Collect every indicator id referenced by any condition in the spec.
 * Used to flag unused indicators and to dim them in the reference panel.
 */
export function collectIndicatorReferences(spec: StrategySpec): Set<string> {
  const refs = new Set<string>();
  function harvestOperand(op: Operand): void {
    if (!op) return;
    if (op.kind === 'IndicatorRef') refs.add(op.indicator);
    if (op.kind === 'Subtract') {
      harvestOperand(op.left);
      harvestOperand(op.right);
    }
  }
  function harvestCond(c: Condition): void {
    if (c.kind === 'FreshCross') {
      refs.add(c.left);
      refs.add(c.right);
    } else if (c.kind === 'IndicatorBetween') {
      refs.add(c.indicator);
    } else if (c.kind === 'IndicatorComparison') {
      harvestOperand(c.left);
      harvestOperand(c.right);
    }
  }
  (spec.entry?.conditions ?? []).forEach((c) => harvestCond(c as Condition));
  (spec.exit?.conditions ?? []).forEach((c) => harvestCond(c as Condition));
  (spec.survival ?? []).forEach((r) =>
    (r.when?.conditions ?? []).forEach((c) => harvestCond(c as Condition)),
  );
  return refs;
}
