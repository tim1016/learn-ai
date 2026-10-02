import { describe, expect, it } from 'vitest';

import { evidenceRows, expectedDefault, factualNote, researchRow } from './golden-search-decision';
import { examView, protocol } from './testing/fixtures';

describe('golden-search-decision', () => {
  it('prefills the factual reason only for a judged final test the server names no weakness in', () => {
    expect(factualNote(examView())).toContain('meets the stated rules on a confirmatory final test');
    expect(factualNote(examView({ outcome: null, checks: [] }))).toBe('');
  });

  it('a weakness the server names reads as weak evidence, even beside a passing outcome', () => {
    const exam = examView({ weakness: [{ code: 'EXPOSURE_HISTORY_UNKNOWN', text: 'the unknown history of this test interval' }] });

    expect(factualNote(exam)).toBe('');
    expect(researchRow(exam).tone).toBe('warn');
  });

  it('a missing outcome is never read as a pass, even on an untouched interval', () => {
    const row = researchRow(examView({ outcome: null, checks: [] }));

    expect(row.chip).toBe('No outcome recorded');
    expect(row.tone).toBe('warn');
    expect(row.detail).toBe('The final test did not produce a judged result.');
  });

  it('a check that is not available stops a pass from reading as clean', () => {
    const exam = examView({
      outcome: 'not_enough_evidence',
      checks: [{ code: 'BEATS_INCUMBENT', label: 'At least the incumbent', status: 'not_available', detail: 'The incumbent run failed.' }],
    });

    expect(researchRow(exam).detail).toBe('The incumbent run failed.');
  });

  it('the repeatability and acceptance rows follow the approval state', () => {
    const chips = (state: Parameters<typeof evidenceRows>[0]): string[] => evidenceRows(state, examView()).map((row) => row.chip);

    expect(chips('awaiting_review')).toEqual(['Meets the stated rules', 'Built during approval', 'Missing · Python-only', 'Awaiting your approval']);
    expect(chips('qualification_failed')[1]).toBe('Proof failed');
    expect(chips('approved')).toEqual(['Meets the stated rules', 'Verified', 'Missing · Python-only', 'Accepted · Manual override']);
  });

  it('expects to replace the frozen incumbent’s qualification, or no default for registry settings', () => {
    expect(expectedDefault({ protocol: protocol() })).toBeNull();
    expect(expectedDefault({ protocol: protocol({ incumbent: { source: 'qualification', qualification_id: 'gq-7', params: { symbol: 'SPY' } } }) })).toBe('gq-7');
  });
});
