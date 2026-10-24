import { describe, expect, it } from 'vitest';

import { inputsFromSavedRun } from '../../components/strategy-lab/strategy-lab.models';
import type { WalkForwardStudyDetail } from '../../components/walk-forward-study/walk-forward-study.types';
import { registryEntry, goldenDeclaration, strategyInfo } from '../testing/fixtures';
import { handoffFromBacktest, handoffFromSweep, handoffFromWalkForward } from '../explore/handoff/explore-handoff';
import { EMA, savedRun, savedSearch, sweepRequest, sweepableEma } from '../explore/testing/explore-fixtures';
import { goldenHandoffOf, windowsOverlap } from './golden-handoff';

const NO_RANGE = { symbol: '', from: '', to: '', resolution: 'minute' } as const;

/** The declaration the plan form takes its knobs from: Golden Search searches the two EMA lengths. */
function declaration() {
  const [fast] = goldenDeclaration().knobs;
  return goldenDeclaration({
    knobs: [
      fast,
      { ...fast, name: 'slow_period', label: 'Slow EMA length', domain_low: 3, domain_high: 60, default_value: 10 },
    ],
  });
}

function fromSweep(request = sweepRequest()) {
  return goldenHandoffOf(handoffFromSweep({ search: savedSearch({ request }), strategy: strategyInfo(), entry: registryEntry(), destination: 'golden' }), declaration());
}

function fromRun(overrides: Parameters<typeof savedRun>[0] = {}) {
  const run = savedRun(overrides);
  return goldenHandoffOf(
    handoffFromBacktest({ run, inputs: inputsFromSavedRun(run, NO_RANGE), strategy: sweepableEma(), entry: registryEntry(), destination: 'golden' }),
    declaration(),
  );
}

function fromWalkForward(overrides: Partial<WalkForwardStudyDetail> = {}) {
  const study = {
    id: 'wf-0001',
    strategy_key: EMA,
    symbol: 'SPY',
    status: 'completed',
    incomplete: false,
    fold_count: 4,
    completed_folds: 4,
    failed_folds: 0,
    request: { ...sweepRequest(), training_months: 9, test_months: 3 },
    ...overrides,
  } as WalkForwardStudyDetail;
  return goldenHandoffOf(handoffFromWalkForward({ study, strategy: strategyInfo(), entry: registryEntry() }), declaration());
}

const gap = (handoff: { unsupported: readonly { field: string; reason: string }[] }, field: string) => handoff.unsupported.find((item) => item.field === field)?.reason;

describe('a golden plan handed over from a sweep', () => {
  it('turns the grid into the plan’s own edits: a varied setting is searched over its range, a fixed one is held', () => {
    const handoff = fromSweep();

    expect(handoff.symbol).toBe('SPY');
    expect(handoff.edits).toEqual(
      expect.arrayContaining([
        { kind: 'knob-mode', name: 'fast_period', mode: 'search' },
        { kind: 'knob-number', name: 'fast_period', field: 'low', raw: '5' },
        { kind: 'knob-number', name: 'fast_period', field: 'high', raw: '9' },
        { kind: 'knob-number', name: 'fast_period', field: 'step', raw: '2' },
        { kind: 'knob-mode', name: 'slow_period', mode: 'fixed' },
        { kind: 'knob-number', name: 'slow_period', field: 'fixed_value', raw: '21' },
      ]),
    );
  });

  it('carries costs, capital and fill mode, and nothing that would pick the final interval or an exploratory leader', () => {
    const handoff = fromSweep();

    expect(handoff.edits).toEqual(
      expect.arrayContaining([
        { kind: 'number', field: 'commission_per_order', raw: '0.5' },
        { kind: 'number', field: 'slippage_per_share', raw: '0.01' },
        { kind: 'number', field: 'initial_cash', raw: '50000' },
        { kind: 'fill-mode', fillMode: 'decision_minute_open' },
      ]),
    );
    // No edit sets a date, a range, a seed or an objective: those are Golden Search's own to lay out.
    expect(handoff.edits.map((edit) => edit.kind)).not.toContain('range');
    expect(handoff.edits.map((edit) => edit.kind)).not.toContain('objective');
    expect(gap(handoff, 'Leader and cell results')).toContain('never starts from the sweep’s leader');
    expect(gap(handoff, 'Window')).toContain('never takes an explored window as its study range');
    expect(gap(handoff, 'Ranking')).toContain('Golden Search starts at its own objective');
    expect(handoff.destination).toBe('golden');
  });

  it('records the explored window as viewed, in the source’s own words', () => {
    expect(fromSweep().viewed).toEqual({ from: '2026-01-02', to: '2026-06-30', source: 'sweep grid-sea' });
  });

  it('names a setting Golden Search does not search, and one it declares that the sweep never set', () => {
    const handoff = fromSweep(sweepRequest({ param_ranges: { gap: { type: 'value_list', values: [0.2] }, fast_period: { type: 'value_list', values: [5, 8] } } }));

    expect(gap(handoff, 'Setting gap')).toContain('Golden Search declares no search');
    expect(handoff.toChoose.find((item) => item.field === 'Settings the source did not set')?.reason).toContain('Slow EMA length');
    // A list of values has no step, so the plan searches their span at its own step.
    expect(gap(handoff, 'Fast EMA length')).toContain('searches their span, 5 to 8');
  });

  it('carries a range wider than the declared domain as it is, and says the plan will refuse it', () => {
    const handoff = fromSweep(sweepRequest({ param_ranges: { fast_period: { type: 'low_high_step', low: 2, high: 90, step: 4 } } }));

    expect(handoff.edits).toContainEqual({ kind: 'knob-number', name: 'fast_period', field: 'high', raw: '90' });
    expect(gap(handoff, 'Fast EMA length')).toContain('outside the range Golden Search declares for it, 2 to 30');
  });
});

describe('a golden plan handed over from a backtest', () => {
  it('carries the symbol and costs only: one run is a point, not a range', () => {
    const handoff = fromRun();

    expect(handoff.symbol).toBe('QQQ');
    expect(handoff.edits.map((edit) => edit.kind)).not.toContain('knob-mode');
    expect(handoff.edits).toEqual(
      expect.arrayContaining([
        { kind: 'number', field: 'commission_per_order', raw: '0.35' },
        { kind: 'number', field: 'initial_cash', raw: '75000' },
        { kind: 'fill-mode', fillMode: 'next_bar_open' },
      ]),
    );
    expect(gap(handoff, 'Settings')).toContain('carried neither as a range nor as a starting point');
    expect(gap(handoff, 'Window')).toContain('2026-03-02 to 2026-04-02');
    expect(handoff.viewed).toEqual({ from: '2026-03-02', to: '2026-04-02', source: 'backtest run #91' });
  });

  it('applies no cost the run did not record, and says the plan keeps its own', () => {
    const handoff = fromRun({ requestedEngine: 'both', commissionPerOrder: null });

    expect(handoff.edits.find((edit) => edit.kind === 'number' && edit.field === 'commission_per_order')).toBeUndefined();
    expect(handoff.edits.find((edit) => edit.kind === 'number' && edit.field === 'slippage_per_share')).toBeUndefined();
    expect(gap(handoff, 'Commission')).toContain('keeps Golden Search’s own commission');
    expect(gap(handoff, 'Slippage')).toContain('keeps Golden Search’s own slippage');
  });
});

describe('a golden plan handed over from a walk-forward study', () => {
  it('carries the grid and the fold lengths, and not the folds, winners or verdict', () => {
    const handoff = fromWalkForward();

    expect(handoff.edits).toEqual(
      expect.arrayContaining([
        { kind: 'number', field: 'training_months', raw: '9' },
        { kind: 'number', field: 'test_months', raw: '3' },
        { kind: 'knob-number', name: 'fast_period', field: 'high', raw: '9' },
      ]),
    );
    expect(gap(handoff, 'Folds, winners and verdict')).toContain('never starts from the study’s winners');
    expect(handoff.viewed.source).toBe('walk-forward study wf-0001');
  });

  it('says so when the study did not finish', () => {
    const handoff = fromWalkForward({ incomplete: true, status: 'cancelled', completed_folds: 2 });

    expect(gap(handoff, 'Unfinished folds')).toContain('2 folds not completed');
  });
});

describe('windowsOverlap', () => {
  it.each([
    ['2026-01-02', '2026-06-30', '2026-06-30', '2026-09-30', true],
    ['2026-01-02', '2026-06-29', '2026-06-30', '2026-09-30', false],
    ['2026-07-01', '2026-08-01', '2026-06-30', '2026-09-30', true],
    ['2026-10-01', '2026-12-01', '2026-06-30', '2026-09-30', false],
  ])('%s to %s against %s to %s: %s', (from, to, otherFrom, otherTo, overlaps) => {
    expect(windowsOverlap({ from, to }, otherFrom, otherTo)).toBe(overlaps);
  });
});
