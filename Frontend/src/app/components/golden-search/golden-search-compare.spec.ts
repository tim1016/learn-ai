import { describe, expect, it } from 'vitest';

import { candidateRows, initialRowKey, rowKeyFor, selectedCaption } from './golden-search-compare';
import { evidenceCandidate, metrics } from './testing/fixtures';

describe('candidateRows', () => {
  it('reads the searches first and the frozen incumbent last, whatever order the server sent', () => {
    const rows = candidateRows([evidenceCandidate('incumbent'), evidenceCandidate('recent'), evidenceCandidate('all_period')]);

    expect(rows.map((row) => row.key)).toEqual(['all_period', 'recent', 'incumbent']);
    expect(rows.map((row) => row.origin)).toEqual(['All-period search', 'Most recent training window', 'Frozen incumbent']);
  });

  it('folds a recent fit that is the all-period settings into the all-period row', () => {
    const rows = candidateRows([evidenceCandidate('all_period', { same_as: ['recent'] }), evidenceCandidate('recent', { same_as: ['all_period'] }), evidenceCandidate('incumbent')]);

    expect(rows.map((row) => row.key)).toEqual(['all_period', 'incumbent']);
    expect(rows[0].sameAs).toEqual(['Recent fit']);
    expect(rowKeyFor(rows, 'recent')).toBe('all_period');
  });

  it('a fit that is the incumbent’s settings becomes the incumbent’s row, even when only one side says so', () => {
    const rows = candidateRows([evidenceCandidate('all_period', { same_as: ['incumbent'] }), evidenceCandidate('recent'), evidenceCandidate('incumbent')]);

    expect(rows.map((row) => row.key)).toEqual(['recent', 'incumbent']);
    expect(rows[1].sameAs).toEqual(['All-period fit']);
    expect(initialRowKey(rows, null)).toBe('incumbent');
  });

  it('puts each flag beside the number it is about, and shows a failed run’s error instead of numbers', () => {
    const recent = evidenceCandidate('recent', {
      flags: [
        { code: 'DRAWDOWN_ABOVE_CEILING', text: 'Above 12% limit' },
        { code: 'TOO_FEW_TRADES', text: 'Below 30 trades' },
        { code: 'EDGE_OF_RANGE', text: 'At the edge of the range' },
      ],
    });
    const failed = evidenceCandidate('all_period', { development_metrics: metrics({ status: 'failed', error: 'no data for 2024-03-01' }) });

    const [allPeriod, recentRow] = candidateRows([failed, recent]);

    expect(recentRow.drawdownFlags.map((f) => f.text)).toEqual(['Above 12% limit']);
    expect(recentRow.tradeFlags.map((f) => f.text)).toEqual(['Below 30 trades']);
    expect(recentRow.otherFlags.map((f) => f.text)).toEqual(['At the edge of the range']);
    expect(allPeriod.failure).toBe('no data for 2024-03-01');
    expect([allPeriod.netReturn, allPeriod.worstFall, allPeriod.trades, allPeriod.sharpe]).toEqual(['—', '—', '—', '—']);
  });

  it('opens on the study’s locked pick, else the all-period fit', () => {
    const rows = candidateRows([evidenceCandidate('all_period'), evidenceCandidate('recent'), evidenceCandidate('incumbent')]);

    expect(initialRowKey(rows, 'recent')).toBe('recent');
    expect(initialRowKey(rows, null)).toBe('all_period');
  });
});

describe('selectedCaption', () => {
  it('joins the server sentences with one full stop each, and leaves out an empty fixed sentence', () => {
    expect(selectedCaption(evidenceCandidate('all_period'))).toEqual({
      label: 'Selected: All-period fit.',
      detail: 'Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars. Normalized gap fixed at 0 bps.',
    });
    expect(selectedCaption(evidenceCandidate('recent', { params_sentence: 'Gap $0.10.', fixed_sentence: '' })).detail).toBe('Gap $0.10.');
  });
});
