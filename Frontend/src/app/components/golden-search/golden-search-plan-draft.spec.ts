import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../../shared/date/et-midnight';
import { applyPlanEdit, byImportance, draftFromDefaults, draftFromProtocol, draftLayout, knobProblemKey, numberProblemKey, RANGE_PROBLEM_KEY, wireProtocol, withImportance, withServerDates, type PlanDraft } from './golden-search-plan-draft';
import importanceOrder from '@repo-contracts/fixtures/golden-search-importance-order-v1.json';
import { percentInputValue } from './golden-search-display';
import { defaults, emaCapability, protocol } from './testing/fixtures';

function draft(): PlanDraft {
  return draftFromProtocol(protocol());
}

describe('applyPlanEdit', () => {
  it('keeps a picked range beside the plan, whose dates wait for the server to lay them out inside it', () => {
    const counted: PlanDraft = { ...draft(), finalMonths: 3 };
    const ranged = applyPlanEdit(counted, { kind: 'range', from: '2024-09-19', to: '2026-09-18' }, emaCapability());

    expect(ranged.range).toEqual({ from: '2024-09-19', to: '2026-09-18' });
    expect(ranged.protocol).toEqual(counted.protocol);
    // The last day is included, so the range ends at the ET midnight after it.
    expect(draftLayout(ranged)).toEqual({ final_months: 3, training_months: 6, test_months: 2, range_start_ms: etMidnightMs('2024-09-19'), range_end_ms: etMidnightMs('2026-09-19') });
  });

  it('holds the plan on a range with a day the picker cannot use: nothing is laid out and the dates stay', () => {
    const ranged = applyPlanEdit({ ...draft(), finalMonths: 3 }, { kind: 'range', from: '2024-09-19', to: '2026-09-18' }, emaCapability());
    const unusable = applyPlanEdit(ranged, { kind: 'range', from: '', to: '2026-09-18' }, emaCapability());

    expect(unusable.problems.get(RANGE_PROBLEM_KEY)).toBe('Enter a first and a last day.');
    expect(unusable.range).toEqual(ranged.range);
    expect(draftLayout(unusable)).toBeNull();
    // A usable range clears it.
    const again = applyPlanEdit(unusable, { kind: 'range', from: '2025-09-19', to: '2026-09-18' }, emaCapability());
    expect(again.problems.has(RANGE_PROBLEM_KEY)).toBe(false);
    expect(draftLayout(again)?.range_start_ms).toBe(etMidnightMs('2025-09-19'));
  });

  it("asks a revised plan for its final test's length before a picked range is laid out", () => {
    // A revised plan's frozen dates carry no month count.
    const ranged = applyPlanEdit(draft(), { kind: 'range', from: '2024-09-19', to: '2026-09-18' }, emaCapability());

    expect(ranged.problems.get(numberProblemKey('final_months'))).toBe("Enter the final test's length in months; the dates are laid out from it.");
    expect(draftLayout(ranged)).toBeNull();
    const counted = applyPlanEdit(ranged, { kind: 'number', field: 'final_months', raw: '3' }, emaCapability());
    expect(counted.problems.size).toBe(0);
    expect(draftLayout(counted)).toEqual({ final_months: 3, training_months: 6, test_months: 2, range_start_ms: etMidnightMs('2024-09-19'), range_end_ms: etMidnightMs('2026-09-19') });
  });

  it('never reads a blank or unreadable number as zero: the protocol keeps its value and a problem is recorded', () => {
    const blank = applyPlanEdit(draft(), { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '' }, emaCapability());

    expect(blank.protocol.knobs.find((k) => k.name === 'fast_period')?.low).toBe(3);
    expect(blank.problems.has(knobProblemKey('fast_period', 'low'))).toBe(true);

    const fixed = applyPlanEdit(blank, { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '4' }, emaCapability());
    expect(fixed.protocol.knobs.find((k) => k.name === 'fast_period')?.low).toBe(4);
    expect(fixed.problems.size).toBe(0);
  });

  it('refuses a fraction for a count instead of sending it, and accepts the whole number', () => {
    const fraction = applyPlanEdit(draft(), { kind: 'number', field: 'training_months', raw: '6.5' }, emaCapability());

    expect(fraction.protocol.training_months).toBe(6);
    expect(fraction.problems.get(numberProblemKey('training_months'))).toBe('Enter a whole number.');

    const whole = applyPlanEdit(fraction, { kind: 'number', field: 'training_months', raw: '9' }, emaCapability());
    expect(whole.protocol.training_months).toBe(9);
    expect(whole.problems.size).toBe(0);
  });

  it('turns the drawdown percent into the fraction of peak equity the protocol carries', () => {
    const next = applyPlanEdit(draft(), { kind: 'number', field: 'drawdown_percent', raw: '12.5' }, emaCapability());

    expect(next.protocol.policy.max_drawdown_ceiling).toBe(0.125);
    expect(applyPlanEdit(draft(), { kind: 'number', field: 'drawdown_percent', raw: 'abc' }, emaCapability()).problems.has(numberProblemKey('drawdown_percent'))).toBe(true);
  });

  it.each([
    ['1.1', 0.011],
    ['12.3', 0.123],
    ['33.3', 0.333],
    ['1e-7', 1e-9],
  ])('freezes a typed drawdown of %s percent as exactly the fraction %s, with no binary noise for the protocol hash', (raw, fraction) => {
    const next = applyPlanEdit(draft(), { kind: 'number', field: 'drawdown_percent', raw }, emaCapability());

    expect(next.protocol.policy.max_drawdown_ceiling).toBe(fraction);
    expect(percentInputValue(next.protocol.policy.max_drawdown_ceiling)).toBe(Number(raw));
  });

  it('gives a knob its declared default step when it becomes searched, under Zoom as under Grid', () => {
    const searched = applyPlanEdit(draft(), { kind: 'knob-mode', name: 'gap_bps', mode: 'search' }, emaCapability());

    expect(searched.protocol.method).toBe('zoom');
    expect(searched.protocol.knobs.find((k) => k.name === 'gap_bps')?.step).toBe(0.5);

    const grid = applyPlanEdit(searched, { kind: 'method', method: 'grid' }, emaCapability());
    expect(grid.protocol.knobs).toEqual(searched.protocol.knobs);
  });

  it('moves the seed with a fixed knob, so the plan never contradicts where its searches start', () => {
    const base = draft();
    const edited = applyPlanEdit(base, { kind: 'knob-number', name: 'gap_bps', field: 'fixed_value', raw: '2' }, emaCapability());

    expect(edited.protocol.knobs.find((k) => k.name === 'gap_bps')?.fixed_value).toBe(2);
    expect(edited.protocol.seed?.['gap_bps']).toBe(2);

    const searched = applyPlanEdit(base, { kind: 'knob-number', name: 'gap', field: 'fixed_value', raw: '0.5' }, emaCapability());
    expect(searched.protocol.seed?.['gap']).toBe(base.protocol.seed?.['gap']);  // a searched knob's fixed value is not its start
    const fixed = applyPlanEdit(searched, { kind: 'knob-mode', name: 'gap', mode: 'fixed' }, emaCapability());
    expect(fixed.protocol.seed?.['gap']).toBe(0.5);
  });

  it('keeps a step the trader set when a knob is held fixed and searched again', () => {
    const edited = applyPlanEdit(draft(), { kind: 'knob-number', name: 'gap', field: 'step', raw: '0.1' }, emaCapability());
    const fixed = applyPlanEdit(edited, { kind: 'knob-mode', name: 'gap', mode: 'fixed' }, emaCapability());
    const again = applyPlanEdit(fixed, { kind: 'knob-mode', name: 'gap', mode: 'search' }, emaCapability());

    expect(again.protocol.knobs.find((k) => k.name === 'gap')?.step).toBe(0.1);
  });

  it('orders the search by importance, highest first, with ties in the strategy order', () => {
    const raised = applyPlanEdit(draft(), { kind: 'knob-importance', name: 'hold_bars', importance: 9 }, emaCapability());
    expect(raised.protocol.knobs.map((k) => k.name)).toEqual(['hold_bars', 'gap', 'rsi_min', 'rsi_max', 'fast_period', 'slow_period', 'gap_bps']);

    const tied = applyPlanEdit(raised, { kind: 'knob-importance', name: 'slow_period', importance: 9 }, emaCapability());
    expect(tied.protocol.knobs.map((k) => k.name).slice(0, 2)).toEqual(['slow_period', 'hold_bars']);
    expect(tied.protocol.knobs.find((k) => k.name === 'slow_period')?.importance).toBe(9);

    const lowered = applyPlanEdit(tied, { kind: 'knob-importance', name: 'gap', importance: 1 }, emaCapability());
    expect(lowered.protocol.knobs.at(-1)?.name).toBe('gap');
  });

  it('shows the search order the server freezes for every pinned case', () => {
    expect(emaCapability().knobs.map((knob) => knob.name)).toEqual(importanceOrder.declared);
    for (const { scores, order } of importanceOrder.cases) {
      const knobs = [...protocol().knobs].reverse().map((knob) => ({ ...knob, importance: scores[knob.name as keyof typeof scores] }));
      expect(byImportance(knobs, emaCapability()).map((knob) => knob.name)).toEqual(order);
    }
  });

  it('ranks a revised legacy plan at the starting importance and leaves a ranked plan in order', () => {
    const legacy = protocol({ knobs: [...protocol().knobs].reverse().map(({ importance: _importance, ...knob }) => knob) });
    const ranked = withImportance(legacy, emaCapability());
    expect(ranked.knobs.map((k) => k.importance)).toEqual(Array(7).fill(5));
    expect(ranked.knobs.map((k) => k.name)).toEqual(emaCapability().knobs.map((k) => k.name));
    const scored = protocol({ knobs: [...protocol().knobs].reverse() });
    expect(withImportance(scored, emaCapability()).knobs.map((k) => k.name)).toEqual(emaCapability().knobs.map((k) => k.name));
  });

  it('adds and removes a pair audit without duplicating it', () => {
    const removed = applyPlanEdit(draft(), { kind: 'pair', pair: ['rsi_min', 'rsi_max'], included: false }, emaCapability());
    expect(removed.protocol.pair_audits).toEqual([['fast_period', 'slow_period']]);

    const again = applyPlanEdit(draft(), { kind: 'pair', pair: ['fast_period', 'slow_period'], included: true }, emaCapability());
    expect(again.protocol.pair_audits).toHaveLength(2);
  });

  it('keeps the final-test month count through other edits and asks for the dates of all three counts', () => {
    const counted: PlanDraft = { ...draft(), finalMonths: 3 };
    const edited = applyPlanEdit(applyPlanEdit(counted, { kind: 'number', field: 'final_months', raw: '4' }, emaCapability()), { kind: 'number', field: 'test_months', raw: '3' }, emaCapability());

    expect(edited.finalMonths).toBe(4);
    expect(draftLayout(edited)).toEqual({ final_months: 4, training_months: 6, test_months: 3 });
    expect(applyPlanEdit(edited, { kind: 'number', field: 'final_months', raw: '2.5' }, emaCapability()).problems.get(numberProblemKey('final_months'))).toBe('Enter a whole number.');
    expect(draftLayout(applyPlanEdit(edited, { kind: 'number', field: 'training_months', raw: '' }, emaCapability()))).toBeNull();
  });

  it('takes only the dates and fold months from the server and keeps every other edit', () => {
    const edited = applyPlanEdit({ ...draft(), finalMonths: 4 }, { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '4' }, emaCapability());
    const laidOut = defaults({ development_start_ms: etMidnightMs('2023-07-01'), development_end_ms: etMidnightMs('2025-12-01'), final_start_ms: etMidnightMs('2025-12-01'), final_end_ms: etMidnightMs('2026-03-28'), training_months: 7, test_months: 2, budget_cap: 1, final_sessions_cut: 2 });

    const merged = withServerDates(edited, laidOut);

    expect(merged.protocol.final_start_ms).toBe(etMidnightMs('2025-12-01'));
    expect(merged.protocol.development_start_ms).toBe(etMidnightMs('2023-07-01'));
    expect(merged.protocol.knobs.find((k) => k.name === 'fast_period')?.low).toBe(4);
    expect(merged.protocol.budget_cap).toBe(5000);
    expect(merged.protocol.training_months).toBe(6);
    expect(merged.finalMonths).toBe(4);
    expect(merged.finalSessionsCut).toBe(2);
    expect(merged.rangeNote).toBeNull();
    expect(Object.keys(merged.protocol)).not.toContain('final_sessions_cut');
  });

  it("takes the server's word for what a picked range leaves out, and drops it when the range changes", () => {
    const ranged = applyPlanEdit({ ...draft(), finalMonths: 3 }, { kind: 'range', from: '2024-09-19', to: '2026-09-18' }, emaCapability());
    const note = 'Development starts on 2024-10-19. The 21 sessions from 2024-09-19 to 2024-10-18 are left out: the months before the final test have to split into 6 months of training and whole tests of 2 months.';

    const laidOut = withServerDates(ranged, defaults({ development_start_ms: etMidnightMs('2024-10-19'), range_note: note }));

    expect(laidOut.rangeNote).toBe(note);
    expect(laidOut.range).toEqual(ranged.range);
    expect(Object.keys(laidOut.protocol)).not.toContain('range_note');
    expect(applyPlanEdit(laidOut, { kind: 'range', from: '2025-09-19', to: '2026-09-18' }, emaCapability()).rangeNote).toBeUndefined();
  });

  it('drops the cut the server made for dates a picked range or a new final-month count replaces', () => {
    const laidOut: PlanDraft = { ...draft(), finalMonths: 3, finalSessionsCut: 3 };
    const ranged = applyPlanEdit(laidOut, { kind: 'range', from: '2024-09-19', to: '2026-09-18' }, emaCapability());
    const recounted = applyPlanEdit(laidOut, { kind: 'number', field: 'final_months', raw: '4' }, emaCapability());

    expect.soft(ranged.finalSessionsCut, 'a picked range drops the cut the server made for the dates it replaces').toBeUndefined();
    expect.soft(recounted.finalMonths, 'a new final-month count takes the new count').toBe(4);
    expect.soft(recounted.finalSessionsCut, "a new final-month count drops the old count's cut until the server lays out the new dates").toBeUndefined();
  });

  it('forgets an unreadable value once its input is no longer shown, so no problem points at a hidden field', () => {
    const blankStep = applyPlanEdit(draft(), { kind: 'knob-number', name: 'fast_period', field: 'step', raw: '' }, emaCapability());
    const single = applyPlanEdit(applyPlanEdit(blankStep, { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '5' }, emaCapability()), { kind: 'knob-number', name: 'fast_period', field: 'high', raw: '5' }, emaCapability());
    expect(single.problems.size).toBe(0);

    const blankLow = applyPlanEdit(draft(), { kind: 'knob-number', name: 'slow_period', field: 'low', raw: '' }, emaCapability());
    const held = applyPlanEdit(blankLow, { kind: 'knob-mode', name: 'slow_period', mode: 'fixed' }, emaCapability());
    expect(held.problems.size).toBe(0);

    const blankHeld = applyPlanEdit(held, { kind: 'knob-number', name: 'slow_period', field: 'fixed_value', raw: '' }, emaCapability());
    expect([...applyPlanEdit(blankHeld, { kind: 'knob-mode', name: 'slow_period', mode: 'search' }, emaCapability()).problems.keys()]).toEqual([]);
    // A shown input keeps its problem.
    expect(blankHeld.problems.has(knobProblemKey('slow_period', 'fixed_value'))).toBe(true);
  });
});

describe('knobs-reset', () => {
  it('restores the knob table, start and pair audits the plan began with, keeping every other edit', () => {
    let edited = applyPlanEdit(draft(), { kind: 'knob-mode', name: 'fast_period', mode: 'fixed' }, emaCapability());
    edited = applyPlanEdit(edited, { kind: 'knob-number', name: 'fast_period', field: 'fixed_value', raw: '7' }, emaCapability());
    edited = applyPlanEdit(edited, { kind: 'knob-importance', name: 'hold_bars', importance: 8 }, emaCapability());
    edited = applyPlanEdit(edited, { kind: 'pair', pair: ['rsi_min', 'rsi_max'], included: false }, emaCapability());
    edited = applyPlanEdit(edited, { kind: 'knob-number', name: 'gap', field: 'low', raw: '' }, emaCapability());
    edited = applyPlanEdit(edited, { kind: 'number', field: 'budget_cap', raw: '' }, emaCapability());

    const reset = applyPlanEdit(edited, { kind: 'knobs-reset' }, emaCapability());

    expect(reset.protocol.knobs).toEqual(protocol().knobs);
    expect(reset.protocol.seed).toEqual(protocol().seed);
    expect(reset.protocol.pair_audits).toEqual(protocol().pair_audits);
    expect([...reset.problems.keys()]).toEqual([numberProblemKey('budget_cap')]);
  });
});

describe('wireProtocol', () => {
  it('sends a knob searched from a value to itself held at that value, moving the start with it', () => {
    const single = applyPlanEdit(applyPlanEdit(draft(), { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '5' }, emaCapability()), { kind: 'knob-number', name: 'fast_period', field: 'high', raw: '5' }, emaCapability());

    const sent = wireProtocol(single.protocol);

    expect(sent.knobs.find((k) => k.name === 'fast_period')).toMatchObject({ mode: 'fixed', fixed_value: 5 });
    expect(sent.seed?.['fast_period']).toBe(5);
    // The draft still shows a searched range: only the plan the server receives holds the knob.
    expect(single.protocol.knobs.find((k) => k.name === 'fast_period')?.mode).toBe('search');
  });

  it('sends a pair audit only while both its knobs vary, and keeps it chosen so it returns when they do', () => {
    const held = applyPlanEdit(draft(), { kind: 'knob-mode', name: 'slow_period', mode: 'fixed' }, emaCapability());

    expect(wireProtocol(held.protocol).pair_audits).toEqual([['rsi_min', 'rsi_max']]);
    expect(held.protocol.pair_audits).toHaveLength(2);
    const again = applyPlanEdit(held, { kind: 'knob-mode', name: 'slow_period', mode: 'search' }, emaCapability());
    expect(wireProtocol(again.protocol).pair_audits).toEqual(protocol().pair_audits);
  });

  it('leaves a plan with nothing to change as it is', () => {
    expect(wireProtocol(protocol())).toEqual(protocol());
  });
});

describe('draftFromDefaults', () => {
  it('starts from the final-test length the server laid out, and keeps the fields that describe the plan out of it', () => {
    const { draft: fresh, incumbentLabel, incumbentSentence } = draftFromDefaults(defaults({ final_months: 6 }));

    expect(fresh.finalMonths).toBe(6);
    expect(draftLayout(fresh)).toEqual({ final_months: 6, training_months: 6, test_months: 2 });
    expect(incumbentLabel).toBe('Registry validated settings');
    expect(incumbentSentence).toBe('Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars');
    expect(fresh.finalSessionsCut).toBe(0);
    expect(Object.keys(fresh.protocol).filter((key) => ['final_months', 'final_sessions_cut', 'range_note', 'incumbent_label', 'incumbent_sentence', 'exposure'].includes(key))).toEqual([]);
    expect(fresh.protocol).toEqual(protocol());
  });
});
