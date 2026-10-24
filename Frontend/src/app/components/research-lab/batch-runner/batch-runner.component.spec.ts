import { computed, signal, type Signal } from '@angular/core';
import { render, screen, waitFor } from '@testing-library/angular';
import { userEvent } from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

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
 * The cross-sectional page: what it sends, and what a finished run tells the reader about its inputs (#3524).
 *
 * `JobsService` is the seam; everything else (the picker, the template, the mapping from the worker's snake-case
 * result) is the real thing, driven the way a person drives it.
 */

const group = (over: Record<string, number> = {}) => ({
  expected_sessions: 148, reused: 70, added: 78, missing: 0, failed: 0,
  building: 0, corrupt: 0, unavailable_from_provider: 0, ...over,
});

const providerInput = (over: Record<string, unknown> = {}) => ({
  kind: 'provider', provider: 'polygon', endpoint: '/v3/reference/options/contracts',
  attempted: 4, failed: 0, observation_digest: 'a'.repeat(64), reproducible: false, ...over,
});

function dataSource(over: { cacheHit?: boolean; failed?: number; coverage?: Record<string, number> } = {}) {
  return {
    kind: 'composite',
    calculation_version: 'cross-sectional-study/1',
    stock_bars: {
      kind: 'lake', provider: 'polygon', price_adjustment_mode: 'polygon_split_adjusted', session_policy: 'regular',
      bar_resolution: 'day', window_start_ms: 1756785600000, window_end_ms: 1764565200000,
      identity_digest: 'b'.repeat(64), universe_snapshot_digest: 'c'.repeat(64),
      coverage: { window: group(over.coverage), artifacts: { fetched: 78, reused: 70, refreshed: 0 } },
      members: [
        { ticker: 'SPY', status: 'admitted' },
        { ticker: 'QQQ', status: 'admitted' },
      ],
    },
    option_inputs: {
      inputs: {
        option_expiries: providerInput(),
        option_contracts: providerInput(),
        option_bars: providerInput({ failed: over.failed ?? 0 }),
      },
      risk_free_rate: { provider: 'fred', days_from_fred: 0, days_from_default: 148, reproducible: false },
      failures_total: over.failed ?? 0,
    },
    cache: { hit: over.cacheHit ?? false, cached_at_ms: over.cacheHit ? 1761000000000 : null },
  };
}

function tickerRow(ticker: string, over: Record<string, unknown> = {}) {
  return {
    ticker, mean_ic: 0.03, ic_t_stat: 1.2, ic_p_value: 0.2, nw_t_stat: 1.1, nw_p_value: 0.25, effective_n: 40,
    is_stationary: true, passed_validation: false, data_points: 74, error: null, validity: 'valid',
    low_confidence: false, exclusion: null, option_inputs: { failures_total: 0 }, ...over,
  };
}

function universe(over: Record<string, unknown> = {}) {
  return {
    requested: ['SPY', 'QQQ'], requested_count: 2, evaluated: ['SPY', 'QQQ'], evaluated_count: 2,
    excluded: [], excluded_count: 0, degraded: [], denominator: 2,
    inclusion_policy: { name: 'valid_members_only', description: 'Statistics count only valid members.' },
    complete: true, ...over,
  };
}

function rawResult(over: Record<string, unknown> = {}) {
  return {
    success: true, status: 'complete', feature_name: 'iv_rank_60', target_type: 'directional',
    tickers_tested: 2, tickers_tested_raw: 2, tickers_valid: 2, tickers_passed: 0, pass_rate: 0,
    cross_sectional_consistent: false, aggregate_ic: 0.03, aggregate_ic_uniform: 0.03,
    validity_summary: { valid: 2, invalid_iv: 0, invalid_data: 0, errored: 0 },
    ticker_results: [tickerRow('SPY'), tickerRow('QQQ')],
    summary: 'Feature iv_rank_60: 2 valid tickers.',
    universe: universe(), data_source: dataSource(),
    ...over,
  };
}

interface JobsServiceMock {
  jobs: Signal<JobState[]>;
  job: (id: string) => JobState | undefined;
  startJob: ReturnType<typeof vi.fn>;
  cancelJob: ReturnType<typeof vi.fn>;
  fetchResult: ReturnType<typeof vi.fn>;
  dismiss: ReturnType<typeof vi.fn>;
}

async function renderPage(result?: Record<string, unknown>) {
  const jobsById = signal<Map<string, JobState>>(new Map());
  const jobs: JobsServiceMock = {
    jobs: computed(() => Array.from(jobsById().values())),
    job: (id) => jobsById().get(id),
    startJob: vi.fn().mockResolvedValue('job-1'),
    cancelJob: vi.fn().mockResolvedValue(undefined),
    fetchResult: vi.fn().mockResolvedValue(result ?? rawResult()),
    dismiss: vi.fn(),
  };
  const view = await render(BatchRunnerComponent, {
    providers: [
      { provide: JobsService, useValue: jobs },
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
  });
  const finish = (status: JobState['status'], extra: Partial<JobState> = {}) =>
    jobsById.set(new Map([['job-1', { id: 'job-1', type: 'cross_sectional', status, recentLogs: [], logSeq: 0, ...extra }]]));
  return { view, jobs, finish, user: userEvent.setup() };
}

const runButton = () => screen.getByRole('button', { name: /run cross-sectional study/i });

describe('BatchRunnerComponent', () => {
  it('asks for daily bars and does not bypass the stored result unless told to', async () => {
    const { jobs, user } = await renderPage();

    await user.click(runButton());

    expect(jobs.startJob).toHaveBeenCalledOnce();
    const [type, payload] = jobs.startJob.mock.calls[0];
    expect(type).toBe('cross_sectional');
    expect(payload).toMatchObject({ timespan: 'day', multiplier: 1, force: false, symbols: ['SPY', 'QQQ', 'AAPL'] });
  });

  it('offers no resolution choice, since the study only runs on daily bars', async () => {
    const { view } = await renderPage();

    expect(view.container.querySelector('app-sampling-card')).toBeNull();
  });

  it('names the lake for the stock bars and the provider for the option inputs, with the session counts', async () => {
    const { user, finish } = await renderPage();
    await user.click(runButton());
    finish('completed');

    const line = await screen.findByText(/Stock bars: data lake/);

    expect(line.textContent).toContain('polygon split adjusted daily, regular session');
    expect(line.textContent).toContain('148 member-sessions: 70 reused, 78 added, 0 missing, 0 failed');
    expect(line.textContent).toContain('Option inputs: polygon live requests, not archived — 0 failed');
    expect(line.textContent).toContain('risk-free rate: FRED, 148 days on the default');
    expect(screen.queryByTestId('partial-universe')).toBeNull();
    expect(screen.queryByRole('button', { name: /ignoring the stored result/i })).toBeNull();
  });

  it('says how many requested members are in the statistics, and why each other is not', async () => {
    const { user, finish } = await renderPage(
      rawResult({
        status: 'partial',
        tickers_tested_raw: 3, tickers_valid: 2,
        ticker_results: [
          tickerRow('SPY'), tickerRow('QQQ'),
          tickerRow('IWM', {
            validity: 'invalid_data', error: 'The lake cannot admit this window: 1 of 148 sessions cannot be supplied by the provider: 2025-10-09',
            exclusion: { ticker: 'IWM', stage: 'stock_bars', code: 'data_unavailable_from_provider', reason: 'The lake cannot admit this window: 1 of 148 sessions cannot be supplied by the provider: 2025-10-09', retryable: true },
          }),
        ],
        universe: universe({
          requested: ['SPY', 'QQQ', 'IWM'], requested_count: 3, complete: false,
          excluded: [{ ticker: 'IWM', stage: 'stock_bars', code: 'data_unavailable_from_provider', reason: 'The lake cannot admit this window: 1 of 148 sessions cannot be supplied by the provider: 2025-10-09', retryable: true }],
          excluded_count: 1,
        }),
      }),
    );
    await user.click(runButton());
    finish('completed');

    const banner = await screen.findByTestId('partial-universe');

    expect(banner.textContent).toContain('2 of 3 requested members are in the statistics');
    expect(banner.textContent).toContain('every statistic divides by 2');
    expect(banner.textContent).toMatch(/IWM\s*— The lake cannot admit this window/);
    expect(banner.textContent).toContain('Statistics count only valid members.');
  });

  it('flags a member that is in the statistics but was built on unanswered option requests', async () => {
    const { user, finish } = await renderPage(
      rawResult({
        status: 'partial',
        ticker_results: [tickerRow('SPY'), tickerRow('QQQ', { option_inputs: { failures_total: 3 } })],
        universe: universe({ complete: false, degraded: [{ ticker: 'QQQ', failures_total: 3 }] }),
        data_source: dataSource({ failed: 3 }),
      }),
    );
    await user.click(runButton());
    finish('completed');

    const banner = await screen.findByTestId('partial-universe');

    expect(banner.textContent).toMatch(/QQQ\s*— in the statistics, but 3 option\s+request\(s\) went unanswered/);
    expect(screen.getByText(/3 option request\(s\) went unanswered while this ticker's IV history was built/)).not.toBeNull();
    expect((await screen.findByText(/Stock bars: data lake/)).textContent).toContain('Option inputs: polygon live requests, not archived — 3 failed');
  });

  it('says a result is a stored one and offers to run again without it', async () => {
    const { jobs, user, finish } = await renderPage(rawResult({ data_source: dataSource({ cacheHit: true }) }));
    await user.click(runButton());
    finish('completed');

    const note = await screen.findByTestId('cache-note');
    expect(note.textContent).toContain('Stored result, computed');
    expect(note.textContent).toContain('from the same admitted data');

    jobs.startJob.mockClear();
    await user.click(screen.getByRole('button', { name: /run again, ignoring the stored result/i }));

    expect(jobs.startJob).toHaveBeenCalledOnce();
    expect(jobs.startJob.mock.calls[0][1]).toMatchObject({ force: true, timespan: 'day' });
  });

  it('shows the reason a refused run gives instead of an opaque failure', async () => {
    const { user, finish } = await renderPage();
    await user.click(runButton());

    finish('failed', {
      errorMessage: 'The cross-sectional study runs on daily bars only (its targets look one and five sessions ahead); 1-minute bars were requested. Use daily bars.',
    });

    await waitFor(() => expect(screen.getByText(/runs on daily bars only/)).not.toBeNull());
  });
});
