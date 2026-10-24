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
    const fromBeforeTheLake = { ...RVIV_RESULT, bars: [], data_source: undefined } as never;

    const view = storedStudyView(storedRecord('realized-vs-iv', { result: fromBeforeTheLake }));

    expect(view).toEqual({
      kind: 'unreadable',
      reason: 'this run was saved in a form from before the study read the data lake, which this page can no longer draw',
    });
  });

  it('shows a saved Realized vs IV result without the live marker, which a result only ever had at its own moment', () => {
    const view = storedStudyView(storedRecord('realized-vs-iv'));

    expect(view.kind === 'realized-vs-iv' && view.data.liveIv30.kind).toBe('unavailable');
    expect(view.kind === 'realized-vs-iv' && view.data.N).toBe(6);
  });

  it('reads the bar size, the lake source and the IV input from the saved result, not from the shape of its bars', () => {
    const daily = storedStudyView(storedRecord('realized-vs-iv'));
    const intraday = storedStudyView(
      storedRecord('realized-vs-iv', { result: { ...RVIV_RESULT, bar_size: '15m', hf_session: 'RTH' } as never }),
    );

    expect(daily.kind === 'realized-vs-iv' && daily.data.run).toEqual({ barSize: '1D', tenorDays: 30, hfSession: null });
    expect(intraday.kind === 'realized-vs-iv' && intraday.data.run.barSize).toBe('15m');
    expect(daily.kind === 'realized-vs-iv' && daily.data.dataSource.provider).toBe('polygon');
    expect(daily.kind === 'realized-vs-iv' && daily.data.ivInput.source).toBe('absent');
  });
});
