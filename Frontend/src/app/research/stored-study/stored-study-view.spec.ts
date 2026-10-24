import { describe, expect, it } from 'vitest';

import { storedStudyView } from './stored-study-view';
import { STORED_STUDY_TYPE_IDS } from './stored-study';
import { RVIV_RESULT, storedRecord } from './testing/stored-study-fixtures';

describe('storedStudyView', () => {
  it.each([
    ['feature-study', 'feature'],
    ['signal-study', 'signal'],
    ['cross-sectional-study', 'cross-sectional'],
    ['indicator-reliability', 'indicator-reliability'],
    ['realized-vs-iv', 'realized-vs-iv'],
  ] as const)('maps a saved %s result for the view its study draws a live one with', (study, kind) => {
    expect(storedStudyView(storedRecord(study)).kind).toBe(kind);
  });

  it('covers every kind of saved run the data plane serves', () => {
    for (const study of STORED_STUDY_TYPE_IDS) {
      expect(['none', 'unreadable'], `${study} has a view`).not.toContain(storedStudyView(storedRecord(study)).kind);
    }
  });

  it('maps a result with the study’s own mapper: snake_case in, the live result’s model out', () => {
    const view = storedStudyView(storedRecord('feature-study'));

    expect(view.kind === 'feature' && view.result.meanIC).toBe(0.0123);
    expect(view.kind === 'feature' && view.result.barsUsed).toBe(31_250);
  });

  it.each(['failed', 'cancelled'] as const)('has nothing to draw for a %s run, which left no result', (status) => {
    expect(storedStudyView(storedRecord('feature-study', { status, result: null }))).toEqual({ kind: 'none' });
  });

  it('has nothing to draw for a refusal the study parked as the result of a failed run', () => {
    const refusal = { success: false, status: 'refused', error: 'The lake lacks 3 sessions.' } as never;

    expect(storedStudyView(storedRecord('feature-study', { status: 'failed', result: refusal }))).toEqual({ kind: 'none' });
  });

  it('says a result is unreadable, with the reason, instead of drawing it from a guess', () => {
    const noBars = { ...RVIV_RESULT, bars: [] } as never;

    const view = storedStudyView(storedRecord('realized-vs-iv', { result: noBars }));

    expect(view).toEqual({ kind: 'unreadable', reason: 'the saved run holds no bars' });
  });

  it('shows a saved Realized vs IV result without the live marker, which a result only ever had at its own moment', () => {
    const view = storedStudyView(storedRecord('realized-vs-iv'));

    expect(view.kind === 'realized-vs-iv' && view.data.liveIv30).toBeNull();
    expect(view.kind === 'realized-vs-iv' && view.data.N).toBe(6);
  });

  it('formats the chart axis for the bars the run held, whatever size they were sent as', () => {
    const fiveMinutes = RVIV_RESULT.bars.map((bar, i) => ({ ...bar, ts: RVIV_RESULT.bars[0].ts + i * 300_000 }));
    const intraday = storedStudyView(storedRecord('realized-vs-iv', { result: { ...RVIV_RESULT, bars: fiveMinutes } as never }));
    const daily = storedStudyView(storedRecord('realized-vs-iv'));

    expect(intraday.kind === 'realized-vs-iv' && intraday.barSize).toBe('15m');
    expect(daily.kind === 'realized-vs-iv' && daily.barSize).toBe('1D');
  });
});
