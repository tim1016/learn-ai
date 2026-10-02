import { describe, expect, it } from 'vitest';

import { metricTexts } from './golden-search-display';
import { metrics } from './testing/fixtures';

describe('metricTexts', () => {
  it('shows a completed run’s signed net return, worst fall, trades and Sharpe', () => {
    expect(metricTexts(metrics({ total_return_pct: 0.087, max_drawdown_pct: 0.064, total_trades: 146, sharpe_ratio: 1.18 }))).toEqual({
      netReturn: '+8.7%',
      worstFall: '6.4%',
      trades: '146',
      sharpe: '1.18',
      failure: null,
    });
    expect(metricTexts(metrics({ total_return_pct: -0.012 })).netReturn).toBe('-1.2%');
  });

  it('reads "—" for a statistic the engine left undefined, never zero', () => {
    const figures = metricTexts(metrics({ total_return_pct: null, max_drawdown_pct: null, sharpe_ratio: null, total_trades: 0 }));

    expect(figures).toEqual({ netReturn: '—', worstFall: '—', trades: '0', sharpe: '—', failure: null });
  });

  it('a failed run reads "—" in every figure and gives its error, or says it failed', () => {
    expect(metricTexts(metrics({ status: 'failed', error: 'engine refused the window', total_trades: 7 }))).toEqual({
      netReturn: '—',
      worstFall: '—',
      trades: '—',
      sharpe: '—',
      failure: 'engine refused the window',
    });
    expect(metricTexts(metrics({ status: 'failed', error: null })).failure).toBe('The run failed.');
  });

  it('a missing result reads "—" in every figure without claiming a failure', () => {
    expect(metricTexts(null)).toEqual({ netReturn: '—', worstFall: '—', trades: '—', sharpe: '—', failure: null });
  });
});
