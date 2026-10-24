import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';

import { JobsService, type JobState } from '../../../services/jobs.service';
import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import {
  fakeEnsureCoverage,
  fakeVendorCatalog,
  provideFakeEnsureCoverage,
  provideFakeVendorCatalog,
} from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { BatchRunnerComponent } from './batch-runner.component';

/**
 * A finished run over three requested members, one of which has no usable
 * implied-volatility history. The worker reports it with a reason and leaves
 * it out of the statistics (the existing inclusion policy), and says how many
 * were requested and how many were valid.
 */
const FINISHED_WITH_ONE_UNAVAILABLE_MEMBER = {
  success: true,
  feature_name: 'iv_rank_60',
  target_type: 'directional',
  tickers_tested: 2,
  tickers_tested_raw: 3,
  tickers_valid: 2,
  tickers_passed: 1,
  pass_rate: 0.5,
  cross_sectional_consistent: false,
  aggregate_ic: 0.0213,
  validity_summary: { valid: 2, invalid_iv: 1, invalid_data: 0, errored: 0 },
  ticker_results: [
    member('SPY', 'valid', true),
    member('QQQ', 'invalid_iv', false, 'No usable option history for this member.'),
    member('AAPL', 'valid', false),
  ],
  summary: '1 of 2 valid members passed.',
};

function member(ticker: string, validity: string, passed: boolean, error: string | null = null) {
  return {
    ticker,
    mean_ic: 0.02,
    ic_t_stat: 1.1,
    ic_p_value: 0.2,
    nw_t_stat: 1.0,
    nw_p_value: 0.3,
    effective_n: 40,
    is_stationary: true,
    passed_validation: passed,
    data_points: 60,
    error,
    validity,
  };
}

describe('BatchRunnerComponent (Cross-sectional study)', () => {
  let fixture: ComponentFixture<BatchRunnerComponent>;
  let component: BatchRunnerComponent;
  let jobsById: ReturnType<typeof signal<Map<string, JobState>>>;
  let jobsMock: {
    jobs: ReturnType<typeof computed<JobState[]>>;
    job: (id: string) => JobState | undefined;
    startJob: ReturnType<typeof vi.fn>;
    cancelJob: ReturnType<typeof vi.fn>;
    fetchResult: ReturnType<typeof vi.fn>;
    dismiss: ReturnType<typeof vi.fn>;
  };

  const root = () => fixture.nativeElement as HTMLElement;

  beforeEach(async () => {
    jobsById = signal(new Map<string, JobState>());
    jobsMock = {
      jobs: computed(() => Array.from(jobsById().values())),
      job: (id) => jobsById().get(id),
      startJob: vi.fn().mockResolvedValue('job-1'),
      cancelJob: vi.fn().mockResolvedValue(undefined),
      fetchResult: vi.fn().mockResolvedValue(FINISHED_WITH_ONE_UNAVAILABLE_MEMBER),
      dismiss: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [BatchRunnerComponent, HttpClientTestingModule],
      providers: [
        { provide: JobsService, useValue: jobsMock },
        provideRouter([]),
        provideFakeTickerCatalog(
          fakeTickerCatalog([
            { symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' },
            { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
            { symbol: 'AAPL', name: 'Apple Inc.', exchange: 'NASDAQ' },
          ]),
        ),
        provideFakeVendorCatalog(fakeVendorCatalog([])),
        provideFakeEnsureCoverage(fakeEnsureCoverage()),
        ...provideFakeDateWorld(),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BatchRunnerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  async function finishRun(): Promise<void> {
    component.jobId.set('job-1');
    jobsById.set(
      new Map([['job-1', { id: 'job-1', type: 'cross_sectional', status: 'completed', recentLogs: [], logSeq: 0 }]]),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  it('keeps a multi-symbol universe, not one instrument, and offers no sampling control (T18)', () => {
    expect.soft(component.range().symbols).toEqual(['SPY', 'QQQ', 'AAPL']);
    expect.soft(root().textContent, 'the picker counts the universe').toContain('3 tickers');
    expect.soft(root().querySelector('app-sampling-card'), 'the resolution control did nothing').toBeNull();
    expect.soft(root().querySelector('[data-testid="study-constraints"]')?.textContent).toContain('daily, fixed');
    expect.soft(root().querySelector('h2')?.textContent).toContain('Universe and window');
  });

  it('shows requested against evaluated, each member and the reason for the one left out (T18)', async () => {
    await finishRun();

    const text = root().textContent?.replace(/\s+/g, ' ') ?? '';
    expect.soft(text, 'valid against requested').toContain('Valid / Tested');
    expect.soft(text).toContain('2 / 3');
    expect.soft(text, 'why one member is not in the statistics').toContain('1 invalid IV');
    for (const ticker of ['SPY', 'QQQ', 'AAPL']) {
      expect.soft(text, `${ticker} has its own row`).toContain(ticker);
    }
    expect.soft(text).toContain('INVALID — IV');
    expect.soft(text, 'the reason travels with the member').toContain('No usable option history for this member.');
  });

  it('starts nothing when the universe, the window or the feature changes', () => {
    component.range.update((r) => ({ ...r, symbols: ['SPY', 'QQQ'], from: '2025-10-01' }));
    component.featureName.set('log_skew');
    component.targetType.set('volatility');
    fixture.detectChanges();

    expect(jobsMock.startJob).not.toHaveBeenCalled();
  });

  it('starts one job when Run study is pressed twice before the page re-renders', async () => {
    const run = root().querySelector<HTMLButtonElement>('button.study-run');
    if (run === null) throw new Error('no Run study button');

    run.click();
    run.click();
    await fixture.whenStable();

    expect(jobsMock.startJob).toHaveBeenCalledTimes(1);
    expect(jobsMock.startJob.mock.calls[0][1]).toMatchObject({ symbols: ['SPY', 'QQQ', 'AAPL'], timespan: 'day' });
  });

  it('leaves a running job running when the study is left', () => {
    component.jobId.set('job-1');
    jobsById.set(
      new Map([['job-1', { id: 'job-1', type: 'cross_sectional', status: 'running', recentLogs: [], logSeq: 0 }]]),
    );
    fixture.detectChanges();

    fixture.destroy();

    expect(jobsMock.cancelJob).not.toHaveBeenCalled();
  });

  it('marks the result as previous once the universe changes', async () => {
    component.range.update((r) => ({ ...r, symbols: [...r.symbols] }));
    await component.runBatch();
    await finishRun();

    component.range.update((r) => ({ ...r, symbols: ['SPY'] }));
    fixture.detectChanges();

    expect(root().textContent).toContain('Previous result');
    expect(root().textContent?.replace(/\s+/g, ' ')).toContain('AAPL, QQQ, SPY');
  });
});
