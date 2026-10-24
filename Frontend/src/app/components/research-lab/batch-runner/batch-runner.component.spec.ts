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

/** The lake block as `stock_bars_block` writes it: the shape every lake study's block has, for a universe. */
function stockBars(over: Record<string, unknown> = {}, counts: Record<string, number> = {}) {
  return {
    kind: 'lake', provider: 'polygon', symbol: 'SPY, QQQ', price_adjustment_mode: 'polygon_split_adjusted',
    session_policy: 'regular', window_start_ms: 1756785600000, window_end_ms: 1764565200000,
    effective_window: { start_ms: 1756785600000, end_ms: 1764565200000 }, warmup_sessions: 0, bar_minutes: null,
    aggregation: 'minute_fold_per_et_session_no_fill', identity_digest: 'b'.repeat(64), calculation_version: 'cross-sectional-study/1',
    snapshot_digest: 'c'.repeat(64),
    coverage: {
      window: group(counts), warmup: group({ expected_sessions: 0, reused: 0, added: 0 }),
      artifacts: { fetched: 78, reused: 70, refreshed: 0 },
    },
    bars_used: 148, cache: { hit: false, cached_at_ms: null },
    members: [{ ticker: 'SPY', status: 'admitted' }, { ticker: 'QQQ', status: 'admitted' }],
    ...over,
  };
}

function optionInputs(over: { failed?: number; defaultDays?: number; fetchFailedDays?: number } = {}) {
  return {
    inputs: {
      option_expiries: providerInput(),
      option_contracts: providerInput(),
      option_bars: providerInput({ failed: over.failed ?? 0 }),
    },
    risk_free_rate: {
      provider: 'fred', days_from_fred: 0, days_from_default: over.defaultDays ?? 148,
      days_fetch_failed: over.fetchFailedDays ?? 0, reproducible: false,
    },
    failures_total: over.failed ?? 0,
  };
}

function dataSource(over: { cacheHit?: boolean; failed?: number; fetchFailedDays?: number; coverage?: Record<string, number> } = {}) {
  return {
    kind: 'composite',
    calculation_version: 'cross-sectional-study/1',
    stock_bars: stockBars(
      { cache: { hit: over.cacheHit ?? false, cached_at_ms: over.cacheHit ? 1761000000000 : null } },
      over.coverage,
    ),
    option_inputs: optionInputs({ failed: over.failed, fetchFailedDays: over.fetchFailedDays }),
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

  it('names the lake for the stock bars on the shared line and the provider for the option inputs apart', async () => {
    const { user, finish } = await renderPage();
    await user.click(runButton());
    finish('completed');

    expect(await screen.findByText('Data source')).not.toBeNull();
    expect(screen.getByText('SPY, QQQ')).not.toBeNull();
    expect(screen.getAllByText('daily bars, folded from 1-minute bars').length).toBeGreaterThan(0);
    expect(screen.getByText('78 added · 70 reused · 0 missing')).not.toBeNull();
    const options = screen.getByText(/Option inputs: polygon live requests/);
    expect(options.textContent).toContain('0 failed');
    expect(options.textContent).toContain('risk-free rate: FRED, 148 days on the default');
    expect(options.textContent).not.toContain('because FRED did not answer');
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
        universe: universe({ complete: false, degraded: [{ ticker: 'QQQ', failures_total: 3, rate_days_fetch_failed: 0 }] }),
        data_source: dataSource({ failed: 3 }),
      }),
    );
    await user.click(runButton());
    finish('completed');

    const banner = await screen.findByTestId('partial-universe');

    expect(banner.textContent).toMatch(/QQQ\s*— in the statistics, but 3 option request\(s\) went unanswered while its IV history was built\./);
    expect(screen.getByText(/3 option request\(s\) went unanswered while this ticker's IV history was built/)).not.toBeNull();
    expect((await screen.findByText(/Option inputs: polygon live requests/)).textContent).toContain('3 failed');
  });

  it('flags a member whose days were priced at the default rate because FRED did not answer', async () => {
    const { user, finish } = await renderPage(
      rawResult({
        status: 'partial',
        universe: universe({ complete: false, degraded: [{ ticker: 'QQQ', failures_total: 0, rate_days_fetch_failed: 74 }] }),
        data_source: dataSource({ fetchFailedDays: 74 }),
      }),
    );
    await user.click(runButton());
    finish('completed');

    const banner = await screen.findByTestId('partial-universe');

    expect(banner.textContent).toMatch(/QQQ\s*— in the statistics, but 74 day\(s\) were priced at the default risk-free rate because FRED did not answer\./);
    expect(banner.textContent).not.toContain('option request(s)');
    expect((await screen.findByText(/Option inputs: polygon live requests/)).textContent).toContain('148 days on the default (74 because FRED did not answer)');
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

  describe('a run that ends without a result', () => {
    const excluded = (ticker: string, over: Record<string, unknown> = {}) => ({
      ticker, stage: 'stock_bars', code: 'data_unavailable_from_provider',
      reason: 'The lake cannot admit this window: 2 of 148 sessions cannot be supplied by the provider', retryable: true, ...over,
    });

    it('shows the lake refusal with its reason, its counts and each session that failed, and names every member', async () => {
      const refusal = {
        success: false, status: 'refused', feature_name: 'iv_rank_60', target_type: 'directional',
        error: 'The lake cannot admit stock bars for any of the 2 requested members. SPY: nothing. QQQ: nothing.',
        refusal: {
          code: 'data_unavailable_from_provider',
          reason: 'The lake cannot admit stock bars for any of the 2 requested members. SPY: nothing. QQQ: nothing.',
          sessions: [{ group: 'window', trading_date_ms: 1756785600000, outcome: 'unavailable_from_provider', reason: 'SPY: provider_no_data', failure_reason: 'provider_no_data' }],
          sessions_total: 1,
          coverage: {
            window: group({ expected_sessions: 296, reused: 0, added: 295, failed: 1, unavailable_from_provider: 1 }),
            warmup: group({ expected_sessions: 0, reused: 0, added: 0 }),
            artifacts: { fetched: 295, reused: 0, refreshed: 0 },
          },
          members: [excluded('SPY'), excluded('QQQ')], members_total: 2,
        },
        universe: universe({
          evaluated: [], evaluated_count: 0, denominator: 0, complete: false,
          excluded: [excluded('SPY'), excluded('QQQ')], excluded_count: 2,
        }),
        data_source: { kind: 'composite', calculation_version: 'cross-sectional-study/1', stock_bars: stockBars({ snapshot_digest: null, effective_window: null }), option_inputs: null },
      };
      const { user, finish } = await renderPage(refusal);
      await user.click(runButton());

      finish('failed', { errorMessage: refusal.error });

      expect(await screen.findByText('No result: the lake could not supply this study its data.')).not.toBeNull();
      expect(screen.getByText(refusal.refusal.reason)).not.toBeNull();
      expect(screen.getByText(/296 sessions · 0 reused · 295 added · 0 missing · 1 failed \(1 unavailable from the provider\)/)).not.toBeNull();
      expect(screen.getByText('SPY: provider_no_data')).not.toBeNull();
      expect(screen.getByText('SPY, QQQ')).not.toBeNull();
      const members = await screen.findByTestId('failed-universe');
      expect(members.textContent).toContain('0 of 2 requested members are in the statistics');
      expect(members.textContent).toMatch(/SPY\s*— The lake cannot admit this window/);
      expect(members.textContent).toMatch(/QQQ\s*— The lake cannot admit this window/);
      expect(screen.queryByText(/Option inputs:/)).toBeNull();
    });

    it('shows a request refused before anything was read with its reason and no data-source line', async () => {
      const reason = 'The cross-sectional study runs on daily bars only (its targets look one and five sessions ahead); 1-minute bars were requested. Use daily bars.';
      const { user, finish } = await renderPage({
        success: false, status: 'refused', error: reason,
        refusal: { code: 'resolution_unsupported', reason, sessions: [], sessions_total: 0, coverage: null, members: [], members_total: 0 },
        universe: universe({ evaluated: [], evaluated_count: 0, denominator: 0, complete: false }),
        data_source: { kind: 'composite', calculation_version: 'cross-sectional-study/1', stock_bars: null, option_inputs: null },
      });
      await user.click(runButton());

      finish('failed', { errorMessage: reason });

      await waitFor(() => expect(screen.getByText(/runs on daily bars only/)).not.toBeNull());
      expect(screen.queryByText('Data source')).toBeNull();
    });

    it('shows why no member was evaluated: the admitted source, the option inputs, and each member with its reason', async () => {
      const reason = 'None of the 2 requested members could be evaluated, so there is no statistic to report. SPY: 30 days is outside the expiry bracket QQQ: 30 days is outside the expiry bracket';
      const stage = (ticker: string) => excluded(ticker, { stage: 'iv_inputs', code: 'iv_build_failed', reason: '30 days is outside the expiry bracket' });
      const { user, finish } = await renderPage({
        success: false, status: 'not_evaluated', feature_name: 'iv_rank_60', target_type: 'directional', error: reason,
        universe: universe({
          evaluated: [], evaluated_count: 0, denominator: 0, complete: false,
          excluded: [stage('SPY'), stage('QQQ')], excluded_count: 2,
        }),
        data_source: { ...dataSource(), option_inputs: optionInputs({ failed: 0 }) },
      });
      await user.click(runButton());

      finish('failed', { errorMessage: reason });

      expect(await screen.findByText(reason)).not.toBeNull();
      expect(screen.queryByText(/No result: the lake could not supply/)).toBeNull();
      expect(screen.getByText('78 added · 70 reused · 0 missing')).not.toBeNull();
      expect(screen.getByText(/Option inputs: polygon live requests/)).not.toBeNull();
      const members = await screen.findByTestId('failed-universe');
      expect(members.textContent).toContain('0 of 2 requested members are in the statistics');
      expect(members.textContent).toMatch(/SPY\s*— 30 days is outside the expiry bracket/);
      expect(members.textContent).toMatch(/QQQ\s*— 30 days is outside the expiry bracket/);
    });

    it('shows only the failure message when the job parked nothing', async () => {
      const { jobs, user, finish } = await renderPage();
      jobs.fetchResult.mockRejectedValue(new Error('no result for this job'));
      await user.click(runButton());

      finish('failed', { errorMessage: 'the worker crashed' });

      expect(await screen.findByText('the worker crashed')).not.toBeNull();
      await waitFor(() => expect(jobs.fetchResult).toHaveBeenCalled());
      expect(screen.queryByTestId('failed-universe')).toBeNull();
      expect(screen.queryByText('Data source')).toBeNull();
    });
  });
});
