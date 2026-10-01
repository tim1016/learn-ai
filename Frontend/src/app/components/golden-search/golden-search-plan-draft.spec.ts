import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../../shared/date/et-midnight';
import { applyPlanEdit, knobProblemKey, numberProblemKey, type PlanDraft } from './golden-search-plan-draft';
import { emaCapability, protocol } from './testing/fixtures';

function draft(): PlanDraft {
  return { protocol: protocol(), problems: new Map() };
}

describe('applyPlanEdit', () => {
  it('moves the development end with the final-test start, so the two intervals always share one boundary', () => {
    const next = applyPlanEdit(draft(), { kind: 'date', field: 'final_start', raw: '2025-11-01' }, emaCapability());

    expect(next.protocol.final_start_ms).toBe(etMidnightMs('2025-11-01'));
    expect(next.protocol.development_end_ms).toBe(next.protocol.final_start_ms);
  });

  it('stores the inclusive last final-test day as the next ET midnight (exclusive end)', () => {
    const next = applyPlanEdit(draft(), { kind: 'date', field: 'final_end', raw: '2026-03-31' }, emaCapability());

    expect(next.protocol.final_end_ms).toBe(etMidnightMs('2026-04-01'));
  });

  it('never reads a blank or unreadable number as zero: the protocol keeps its value and a problem is recorded', () => {
    const blank = applyPlanEdit(draft(), { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '' }, emaCapability());

    expect(blank.protocol.knobs.find((k) => k.name === 'fast_period')?.low).toBe(3);
    expect(blank.problems.has(knobProblemKey('fast_period', 'low'))).toBe(true);

    const fixed = applyPlanEdit(blank, { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '4' }, emaCapability());
    expect(fixed.protocol.knobs.find((k) => k.name === 'fast_period')?.low).toBe(4);
    expect(fixed.problems.size).toBe(0);
  });

  it('turns the drawdown percent into the fraction of peak equity the protocol carries', () => {
    const next = applyPlanEdit(draft(), { kind: 'number', field: 'drawdown_percent', raw: '12.5' }, emaCapability());

    expect(next.protocol.policy.max_drawdown_ceiling).toBe(0.125);
    expect(applyPlanEdit(draft(), { kind: 'number', field: 'drawdown_percent', raw: 'abc' }, emaCapability()).problems.has(numberProblemKey('drawdown_percent'))).toBe(true);
  });

  it('gives every searched knob a grid step (its neighbor step) when Grid is chosen, and leaves fixed knobs without one', () => {
    const next = applyPlanEdit(draft(), { kind: 'method', method: 'grid' }, emaCapability());

    expect(next.protocol.method).toBe('grid');
    expect(next.protocol.knobs.find((k) => k.name === 'gap')?.grid_step).toBe(0.05);
    expect(next.protocol.knobs.find((k) => k.name === 'gap_bps')?.grid_step).toBeNull();
  });

  it('reorders the search order and stops at either end', () => {
    const up = applyPlanEdit(draft(), { kind: 'knob-move', name: 'fast_period', offset: -1 }, emaCapability());
    expect(up.protocol.knobs.map((k) => k.name).slice(0, 4)).toEqual(['gap', 'rsi_min', 'fast_period', 'rsi_max']);

    const first = applyPlanEdit(draft(), { kind: 'knob-move', name: 'gap', offset: -1 }, emaCapability());
    expect(first.protocol.knobs).toEqual(protocol().knobs);
  });

  it('adds and removes a pair audit without duplicating it', () => {
    const removed = applyPlanEdit(draft(), { kind: 'pair', pair: ['rsi_min', 'rsi_max'], included: false }, emaCapability());
    expect(removed.protocol.pair_audits).toEqual([['fast_period', 'slow_period']]);

    const again = applyPlanEdit(draft(), { kind: 'pair', pair: ['fast_period', 'slow_period'], included: true }, emaCapability());
    expect(again.protocol.pair_audits).toHaveLength(2);
  });
});
