import { describe, expect, it } from 'vitest';

import { metricTexts } from './golden-search-display';
import { metrics } from './testing/fixtures';

describe('metricTexts', () => {
  it('turns a metrics reading into display figures: completed, undefined, failed and missing runs', () => {
    expect.soft(metricTexts(metrics({ total_return_pct: 0.087, max_drawdown_pct: 0.064, total_trades: 146, sharpe_ratio: 1.18 })), 'shows a completed run’s signed net return, worst fall, trades and Sharpe').toEqual({
      netReturn: '+8.7%',
      worstFall: '6.4%',
      trades: '146',
      sharpe: '1.18',
      failure: null,
    });
    expect.soft(metricTexts(metrics({ total_return_pct: -0.012 })).netReturn, 'a completed run’s net return carries its sign when negative').toBe('-1.2%');

    const figures = metricTexts(metrics({ total_return_pct: null, max_drawdown_pct: null, sharpe_ratio: null, total_trades: 0 }));
    expect.soft(figures, 'reads "—" for a statistic the engine left undefined, never zero').toEqual({ netReturn: '—', worstFall: '—', trades: '0', sharpe: '—', failure: null });

    expect.soft(metricTexts(metrics({ status: 'failed', error: 'engine refused the window', total_trades: 7 })), 'a failed run reads "—" in every figure and gives its error').toEqual({
      netReturn: '—',
      worstFall: '—',
      trades: '—',
      sharpe: '—',
      failure: 'engine refused the window',
    });
    expect.soft(metricTexts(metrics({ status: 'failed', error: null })).failure, 'a failed run with no error says it failed').toBe('The run failed.');

    expect.soft(metricTexts(null), 'a missing result reads "—" in every figure without claiming a failure').toEqual({ netReturn: '—', worstFall: '—', trades: '—', sharpe: '—', failure: null });
  });
});
