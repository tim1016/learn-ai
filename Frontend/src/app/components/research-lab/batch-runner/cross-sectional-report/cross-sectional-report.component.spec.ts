import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { BatchResearchResult, TickerBatchResult } from '../../../../services/research.service';
import { CrossSectionalReportComponent } from './cross-sectional-report.component';

function member(ticker: string, overrides: Partial<TickerBatchResult> = {}): TickerBatchResult {
  return {
    ticker,
    meanIc: 0.02,
    icTStat: 1.1,
    icPValue: 0.2,
    nwTStat: 1.0,
    nwPValue: 0.3,
    effectiveN: 40,
    isStationary: true,
    passedValidation: true,
    dataPoints: 60,
    validity: 'valid',
    ...overrides,
  };
}

/** Three requested members: one passes, one fails its IC test, one the worker could not run. */
const RESULT: BatchResearchResult = {
  success: true,
  featureName: 'iv_rank_60',
  tickersTested: 2,
  tickersTestedRaw: 3,
  tickersValid: 2,
  tickersPassed: 1,
  passRate: 0.5,
  crossSectionalConsistent: false,
  aggregateIc: 0.0213,
  validitySummary: { valid: 2, invalidIv: 0, invalidData: 0, errored: 1 },
  stageInfo: {
    stage: 1,
    label: 'Weak',
    description: 'Some members pass, the universe does not.',
    nextStageLabel: 'Research candidate',
    failedCriteria: [],
    advanceCriteria: [],
  },
  tickerResults: [
    member('SPY'),
    member('QQQ', { passedValidation: false }),
    member('AAPL', { validity: 'error', passedValidation: false, error: 'Option history request timed out.' }),
  ],
  summary: '1 of 2 valid members passed.',
};

describe('CrossSectionalReportComponent', () => {
  it('shows the stage verdict, valid against requested, and every member with the error of the one that failed to run', async () => {
    await render(CrossSectionalReportComponent, { inputs: { result: RESULT } });

    expect(screen.getByText('Weak')).toBeTruthy();
    expect(screen.getByText('Valid / Tested')).toBeTruthy();
    expect(screen.getByText('2 / 3')).toBeTruthy();
    for (const ticker of ['SPY', 'QQQ', 'AAPL']) {
      expect(screen.getByText(ticker), `${ticker} has its own row`).toBeTruthy();
    }
    expect(screen.getByText('ERROR')).toBeTruthy();
    expect(screen.getByText('Option history request timed out.')).toBeTruthy();
  });
});
