import { describe, expect, it } from 'vitest';

import { evidenceRows, examScored, expectedDefault, factualNote, fitFor, researchRow } from './golden-search-decision';
import { examFit, examView, protocol } from './testing/fixtures';

const UNSCORED = { outcome: null, checks: [] };

describe('golden-search-decision', () => {
  it('prefills the factual reason only for a judged fit the server names no weakness in', () => {
    expect(factualNote(examFit('recent'))).toBe('The Recent fit meets the stated rules on a confirmatory final test. The evidence is Python-only; independent engine agreement is not available.');
    expect(factualNote(examFit('all_period', UNSCORED))).toBe('');
  });

  it('a weakness the server names reads as weak evidence, even beside a passing outcome', () => {
    const exam = examView();
    const fit = examFit('all_period', { weakness: [{ code: 'EXPOSURE_HISTORY_UNKNOWN', text: 'the unknown history of this test interval' }] });

    expect(factualNote(fit)).toBe('');
    expect(researchRow(exam, fit).tone).toBe('warn');
  });

  it('a missing outcome is never read as a pass, even on an untouched interval', () => {
    const row = researchRow(examView(), examFit('all_period', UNSCORED));

    expect(row.chip).toBe('No outcome recorded');
    expect(row.tone).toBe('warn');
    expect(row.detail).toBe('The final test did not produce a judged result.');
  });

  it('a check that is not available stops a pass from reading as clean', () => {
    const fit = examFit('all_period', {
      outcome: 'not_enough_evidence',
      checks: [{ code: 'BEATS_INCUMBENT', label: 'At least the incumbent', status: 'not_available', detail: 'The incumbent run failed.' }],
    });

    expect(researchRow(examView(), fit).detail).toBe('The incumbent run failed.');
  });

  it('the repeatability and acceptance rows follow the approval state', () => {
    const exam = examView();
    const chips = (state: Parameters<typeof evidenceRows>[0]): string[] => evidenceRows(state, exam, exam.fits[0]).map((row) => row.chip);

    expect(chips('awaiting_review')).toEqual(['Meets the stated rules', 'Built during approval', 'Missing · Python-only', 'Awaiting your approval']);
    expect(chips('qualification_failed')[1]).toBe('Proof failed');
    expect(chips('approved')).toEqual(['Meets the stated rules', 'Verified', 'Missing · Python-only', 'Accepted · Manual override']);
  });

  it('the final test has a result only once every fit is scored, and a fit answers to each name that chose its settings', () => {
    expect(examScored(null)).toBe(false);
    expect(examScored(examView({ fits: [examFit(), examFit('recent', UNSCORED)] }))).toBe(false);
    expect(examScored(examView())).toBe(true);
    const twins = examView({ fits: [examFit('all_period', { same_as: ['recent'], label: 'All-period and recent fit' })] });
    expect(fitFor(twins, 'recent')?.key).toBe('all_period');
    expect(fitFor(twins, null)).toBeNull();
  });

  it('expects to replace the frozen incumbent’s qualification, or no default for registry settings', () => {
    expect(expectedDefault({ protocol: protocol() })).toBeNull();
    expect(expectedDefault({ protocol: protocol({ incumbent: { source: 'qualification', qualification_id: 'gq-7', params: { symbol: 'SPY' } } }) })).toBe('gq-7');
  });
});
