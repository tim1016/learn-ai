import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { computed, signal, type Signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor, within } from '@testing-library/angular';
import { userEvent } from '@testing-library/user-event';
import { vi } from 'vitest';

import { environment } from '../../../../environments/environment';
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
import { RECORD_ID, storedRecord } from '../../../research/stored-study/testing/stored-study-fixtures';
import { DataLakeService, type DataLakeRead, type InstrumentDataReport } from '../../../shared/data-lake';
import { TradeBackfillRunner } from '../../../shared/data-lake/trade-backfill-runner';
import { heldReport } from '../../../shared/data-lake/testing/instrument-data-report';
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

/**
 * The lake's count of the sessions a member's window needs (`GET /api/data-lake/instrument-data`), and the shared
 * capture runner. Left alone, the lake holds every session of every member, so only the specs about the count see a
 * warning.
 */
function lakeHoldsEverything(): { provide: unknown; useValue: unknown }[] {
  return [
    { provide: DataLakeService, useValue: { instrumentData: vi.fn().mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: 60 }) }) } },
    { provide: TradeBackfillRunner, useValue: { run: vi.fn() } },
  ];
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
        ...lakeHoldsEverything(),
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

  it('fills the universe, window, feature and target from a saved run, starts nothing, then names it when Run study is pressed', async () => {
    fixture.componentRef.setInput('rerun', RECORD_ID);
    fixture.detectChanges();
    TestBed.inject(HttpTestingController)
      .expectOne(`${environment.pythonServiceUrl}/api/research/study-records/cross-sectional-study/${RECORD_ID}`)
      .flush(storedRecord('cross-sectional-study', { inputs: { ...storedRecord('cross-sectional-study').inputs, symbols: ['iwm', 'dia'], feature_name: 'log_skew', target_type: 'volatility' } as never }));
    await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
    await fixture.whenStable();

    expect(component.range()).toMatchObject({ symbols: ['IWM', 'DIA'], from: '2025-09-02', to: '2025-12-01' });
    expect(component.featureName()).toBe('log_skew');
    expect(component.targetType()).toBe('volatility');
    expect(jobsMock.startJob).not.toHaveBeenCalled();

    await component.runBatch();

    expect(jobsMock.startJob.mock.calls[0][1]).toMatchObject({ symbols: ['IWM', 'DIA'], target_type: 'volatility', rerun_of: RECORD_ID });
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

/**
 * What a finished run tells the reader about its inputs, and why a run that ended without a result has none (#3524).
 *
 * `JobsService` is the seam; everything else (the picker, the template, the mapping from the worker's snake-case
 * result) is the real thing, driven the way a person drives it.
 */

const group = (over: Record<string, number> = {}) => ({
  expected_sessions: 148, reused: 70, added: 78, missing: 0, failed: 0,
  building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 0, ...over,
});

const PRICE_RULE_WORDS =
  'each option is priced at its last regular-hours trade: the close of the latest minute bar that lies wholly inside the scheduled session, kept only with at least 50 contracts traded in regular hours';

/** One member's option inputs as `option_inputs_block` writes it: admitted, read from the archive or kept by this study. */
const optionMember = (ticker: string, over: Record<string, unknown> = {}) => ({
  ticker, status: 'admitted', digest: 'd'.repeat(64), source: 'kept',
  listings: { expected: 21, reused: 0, added: 21 },
  contract_days: { expected: 168, reused: 0, added: 168, from_archive: 0 }, ...over,
});

const archived = (ticker: string) =>
  optionMember(ticker, { source: 'archive', listings: { expected: 21, reused: 21, added: 0 }, contract_days: { expected: 168, reused: 168, added: 0, from_archive: 168 } });

/** The lake block as `stock_bars_block` writes it: the shape every lake study's block has, for a universe. */
function stockBars(over: Record<string, unknown> = {}, counts: Record<string, number> = {}) {
  return {
    kind: 'lake', provider: 'polygon', symbol: 'SPY, QQQ', price_adjustment_mode: 'polygon_split_adjusted',
    session_policy: 'regular', window_start_ms: 1756785600000, window_end_ms: 1764565200000,
    effective_window: { start_ms: 1756785600000, end_ms: 1764565200000 }, warmup_sessions: 0, bar_minutes: null,
    aggregation: 'minute_fold_per_et_session_no_fill', identity_digest: 'b'.repeat(64), calculation_version: 'cross-sectional-study/2',
    snapshot_digest: 'c'.repeat(64), raw_snapshot_digest: 'a'.repeat(64),
    coverage: {
      window: group(counts), warmup: group({ expected_sessions: 0, reused: 0, added: 0 }),
      artifacts: { fetched: 78, reused: 70, refreshed: 0 },
    },
    bars_used: 148, cache: { hit: false, cached_at_ms: null },
    members: [{ ticker: 'SPY', status: 'admitted' }, { ticker: 'QQQ', status: 'admitted' }],
    ...over,
  };
}

function optionInputs(members: Record<string, unknown>[] = [archived('SPY'), archived('QQQ')]) {
  return {
    kind: 'lake', policy: 'lake_option_inputs/v1', price_rule_words: PRICE_RULE_WORDS, digest: 'e'.repeat(64),
    listings: { expected: 42, reused: 42, added: 0 },
    contract_days: { expected: 336, reused: 336, added: 0, from_archive: 336 },
    members,
  };
}

function riskFreeRate(over: { defaultDays?: number; fetchFailedDays?: number } = {}) {
  return {
    kind: 'provider', provider: 'fred', days_from_fred: 0, days_from_default: over.defaultDays ?? 148,
    days_fetch_failed: over.fetchFailedDays ?? 0, values_digest: 'f'.repeat(64),
  };
}

function dataSource(
  over: { cacheHit?: boolean; fetchFailedDays?: number; coverage?: Record<string, number>; members?: Record<string, unknown>[] } = {},
) {
  return {
    kind: 'lake',
    calculation_version: 'cross-sectional-study/2',
    stock_bars: stockBars(
      { cache: { hit: over.cacheHit ?? false, cached_at_ms: over.cacheHit ? 1761000000000 : null } },
      over.coverage,
    ),
    option_inputs: optionInputs(over.members),
    risk_free_rate: riskFreeRate({ fetchFailedDays: over.fetchFailedDays }),
  };
}

function tickerRow(ticker: string, over: Record<string, unknown> = {}) {
  return {
    ticker, mean_ic: 0.03, ic_t_stat: 1.2, ic_p_value: 0.2, nw_t_stat: 1.1, nw_p_value: 0.25, effective_n: 40,
    is_stationary: true, passed_validation: false, data_points: 74, error: null, validity: 'valid',
    low_confidence: false, exclusion: null, ...over,
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

/** What the lake holds of one member's window: every session, unless a spec says some are missing. */
type LakeReading = (symbol: string) => DataLakeRead<InstrumentDataReport>;
const lakeHolds: LakeReading = () => ({ kind: 'ok', value: heldReport({ sessions: 60 }) });

async function renderPage(result?: Record<string, unknown>, lake: LakeReading = lakeHolds) {
  const instrumentData = vi.fn(async (query: { symbol: string }) => lake(query.symbol));
  const captureRun = vi.fn();
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
      { provide: DataLakeService, useValue: { instrumentData } },
      { provide: TradeBackfillRunner, useValue: { run: captureRun } },
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
  });
  const finish = (status: JobState['status'], extra: Partial<JobState> = {}) =>
    jobsById.set(new Map([['job-1', { id: 'job-1', type: 'cross_sectional', status, recentLogs: [], logSeq: 0, ...extra }]]));
  return { view, jobs, finish, instrumentData, captureRun, user: userEvent.setup() };
}

const runButton = () => screen.getByRole('button', { name: /run study/i });

describe('BatchRunnerComponent: where the inputs came from, and why a run has no result (#3524)', () => {
  it('asks for daily bars and does not bypass the stored result unless told to', async () => {
    const { jobs, user } = await renderPage();

    await user.click(runButton());

    expect(jobs.startJob).toHaveBeenCalledOnce();
    const [type, payload] = jobs.startJob.mock.calls[0];
    expect(type).toBe('cross_sectional');
    expect(payload).toMatchObject({ timespan: 'day', multiplier: 1, force: false, symbols: ['SPY', 'QQQ', 'AAPL'] });
  });

  it('names the lake for the stock bars on the shared line, the lake for the option inputs on their own, and FRED apart', async () => {
    const { user, finish } = await renderPage();
    await user.click(runButton());
    finish('completed');

    expect(await screen.findByText('SPY, QQQ'), 'the run\'s own line replaces the study\'s pre-run one').not.toBeNull();
    expect(screen.getAllByText('Data source')).toHaveLength(1);
    expect(screen.getAllByText('daily bars, folded from 1-minute bars').length).toBeGreaterThan(0);
    expect(screen.getByText('78 added · 70 reused · 0 missing')).not.toBeNull();
    const options = screen.getByText(/^Option inputs: /);
    expect(options.textContent).toBe(`Option inputs: SPY, QQQ from the nightly archive. Each option is priced at its last regular-hours trade: the close of the latest minute bar that lies wholly inside the scheduled session, kept only with at least 50 contracts traded in regular hours.`);
    const rate = screen.getByText(/^Risk-free rate: /);
    expect(rate.textContent).toBe("Risk-free rate: FRED, asked live and not archived — 0 days at FRED's rate, 148 on the default.");
    expect(rate.textContent).not.toContain('because FRED did not answer');
    expect(screen.getAllByText('Data lake').length, 'the option inputs say where they are from').toBeGreaterThan(0);
    expect(screen.getByText('FRED (live)')).not.toBeNull();
    expect(document.body.textContent).not.toMatch(/Polygon \(live\)|live requests|not archived — \d+ failed/);
    expect(screen.queryByTestId('partial-universe')).toBeNull();
    expect(screen.queryByRole('button', { name: /ignoring the stored result/i })).toBeNull();
  });

  it('says what this run saved into the lake for a member the archive does not hold, and what it found already kept', async () => {
    const { user, finish } = await renderPage(
      rawResult({
        data_source: dataSource({
          members: [
            archived('SPY'),
            optionMember('AAPL'),
            optionMember('MSFT', { listings: { expected: 21, reused: 21, added: 0 }, contract_days: { expected: 168, reused: 168, added: 0, from_archive: 0 } }),
            optionMember('IWM', { source: 'mixed' }),
          ],
        }),
      }),
    );
    await user.click(runButton());
    finish('completed');

    const options = await screen.findByText(/^Option inputs: /);

    expect(options.textContent).toContain(
      'SPY from the nightly archive; AAPL saved by this run (21 listings, 168 contract days added); MSFT already kept in the lake; IWM from the nightly archive and what this run saved.',
    );
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
    expect(banner.textContent).toMatch(/IWM\s*— stock bars: The lake cannot admit this window/);
    expect(banner.textContent).toContain('Statistics count only valid members.');
  });

  it('puts the members left out above the verdict, which is about the members that remain', async () => {
    const { user, finish } = await renderPage(
      rawResult({
        status: 'partial',
        tickers_tested_raw: 3, tickers_valid: 2,
        ticker_results: [tickerRow('SPY'), tickerRow('QQQ'), tickerRow('IWM', { validity: 'invalid_data', error: 'No bars before 2025-10-09.' })],
        universe: universe({
          requested: ['SPY', 'QQQ', 'IWM'], requested_count: 3, complete: false,
          excluded: [{ ticker: 'IWM', stage: 'stock_bars', code: 'data_unavailable_from_provider', reason: 'No bars before 2025-10-09.', retryable: true }],
          excluded_count: 1,
        }),
        stage_info: {
          stage: 1, label: 'Stage 1', description: 'Some signal.', next_stage_label: 'Stage 2', failed_criteria: [],
          advance_criteria: [{ name: 'Aggregate IC', description: 'Needs more.', current_value: 0.02, required_repr: '>= 0.03', met: false }],
        },
      }),
    );
    await user.click(runButton());
    finish('completed');

    const banner = await screen.findByTestId('partial-universe');

    const verdict = document.querySelector('.verdict-card');
    expect(verdict, 'the verdict is on the page').not.toBeNull();
    expect(banner.compareDocumentPosition(verdict as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('says why a member was left out for want of option inputs, at its own stage, and lists it as excluded beside the admitted ones', async () => {
    const reason = 'The lake cannot supply the option inputs: 2 of 336 option inputs are not in the lake: 2025-10-09 (O:IWM251017C00230000: the contract is not in that day\'s contract listing)';
    const refused = {
      ticker: 'IWM', status: 'excluded', code: 'data_missing', reason, retryable: false, issues_total: 2,
      issues: [{
        kind: 'contract_day', trading_date_ms: 1760031000000, subject: 'O:IWM251017C00230000', outcome: 'missing',
        reason: 'O:IWM251017C00230000: the contract is not in that day\'s contract listing', failure_reason: 'not_listed', retryable: false,
      }],
    };
    const { user, finish } = await renderPage(
      rawResult({
        status: 'partial',
        tickers_tested_raw: 3, tickers_valid: 2,
        ticker_results: [tickerRow('SPY'), tickerRow('QQQ'), tickerRow('IWM', { validity: 'invalid_data', error: reason })],
        universe: universe({
          requested: ['SPY', 'QQQ', 'IWM'], requested_count: 3, complete: false,
          excluded: [{ ticker: 'IWM', stage: 'option_inputs', code: 'data_missing', reason, retryable: false }], excluded_count: 1,
        }),
        data_source: dataSource({ members: [archived('SPY'), archived('QQQ'), refused] }),
      }),
    );
    await user.click(runButton());
    finish('completed');

    const banner = await screen.findByTestId('partial-universe');

    expect(banner.textContent).toMatch(/IWM\s*— option inputs: The lake cannot supply the option inputs/);
    expect((await screen.findByText(/^Option inputs: /)).textContent).toContain('SPY, QQQ from the nightly archive.');
    expect(screen.getByText(/^Option inputs: /).textContent, 'the left-out member is not claimed as read from anywhere').not.toContain('IWM');
  });

  it('flags a member whose days were priced at the default rate because FRED did not answer', async () => {
    const { user, finish } = await renderPage(
      rawResult({
        status: 'partial',
        universe: universe({ complete: false, degraded: [{ ticker: 'QQQ', rate_days_fetch_failed: 74 }] }),
        data_source: dataSource({ fetchFailedDays: 74 }),
      }),
    );
    await user.click(runButton());
    finish('completed');

    const banner = await screen.findByTestId('partial-universe');

    expect(banner.textContent).toMatch(/QQQ\s*— in the statistics, but 74 day\(s\) were priced at the default risk-free rate because FRED did not answer\./);
    expect(banner.textContent).not.toContain('option request(s)');
    expect((await screen.findByText(/^Risk-free rate: /)).textContent).toContain('148 on the default (74 because FRED did not answer)');
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
        data_source: { kind: 'lake', calculation_version: 'cross-sectional-study/2', stock_bars: stockBars({ snapshot_digest: null, effective_window: null }), option_inputs: null, risk_free_rate: null },
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
      expect(members.textContent).toMatch(/SPY\s*— stock bars: The lake cannot admit this window/);
      expect(members.textContent).toMatch(/QQQ\s*— stock bars: The lake cannot admit this window/);
      expect(screen.queryByText(/Option inputs:/)).toBeNull();
    });

    it('shows a request refused before anything was read with its reason and no data-source line', async () => {
      const reason = 'The cross-sectional study runs on daily bars only (its targets look one and five sessions ahead); 1-minute bars were requested. Use daily bars.';
      const { user, finish } = await renderPage({
        success: false, status: 'refused', error: reason,
        refusal: { code: 'resolution_unsupported', reason, sessions: [], sessions_total: 0, coverage: null, members: [], members_total: 0 },
        universe: universe({ evaluated: [], evaluated_count: 0, denominator: 0, complete: false }),
        data_source: { kind: 'lake', calculation_version: 'cross-sectional-study/2', stock_bars: null, option_inputs: null, risk_free_rate: null },
      });
      await user.click(runButton());

      finish('failed', { errorMessage: reason });

      await waitFor(() => expect(screen.getByText(/runs on daily bars only/)).not.toBeNull());
      expect(screen.queryByText(/added · .* reused/), 'nothing was read, so no counts are claimed').toBeNull();
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
        data_source: dataSource(),
      });
      await user.click(runButton());

      finish('failed', { errorMessage: reason });

      expect(await screen.findByText(reason)).not.toBeNull();
      expect(screen.queryByText(/No result: the lake could not supply/)).toBeNull();
      expect(screen.getByText('78 added · 70 reused · 0 missing')).not.toBeNull();
      expect(screen.getByText(/^Option inputs: /)).not.toBeNull();
      expect(screen.getByText(/^Risk-free rate: /)).not.toBeNull();
      const members = await screen.findByTestId('failed-universe');
      expect(members.textContent).toContain('0 of 2 requested members are in the statistics');
      expect(members.textContent).toMatch(/SPY\s*— IV history: 30 days is outside the expiry bracket/);
      expect(members.textContent).toMatch(/QQQ\s*— IV history: 30 days is outside the expiry bracket/);
    });

    it('shows only the failure message when the job parked nothing', async () => {
      const { jobs, user, finish } = await renderPage();
      jobs.fetchResult.mockRejectedValue(new HttpErrorResponse({ status: 404, statusText: 'Not Found' }));
      await user.click(runButton());

      finish('failed', { errorMessage: 'the worker crashed' });

      expect(await screen.findByText('the worker crashed')).not.toBeNull();
      await waitFor(() => expect(jobs.fetchResult).toHaveBeenCalled());
      expect(screen.queryByTestId('failed-universe')).toBeNull();
      expect(screen.queryByText(/added · .* reused/), 'no run read anything, so no counts are claimed').toBeNull();
      expect(screen.queryByText(/could not be loaded/), 'a missing result is the expected answer, not a second failure').toBeNull();
    });

    it('says so when the failed job\'s result could not be read for any reason other than there being none', async () => {
      const { jobs, user, finish } = await renderPage();
      jobs.fetchResult.mockRejectedValue(
        new HttpErrorResponse({ status: 500, statusText: 'Internal Server Error', url: '/api/jobs/job-1/result' }),
      );
      await user.click(runButton());

      finish('failed', { errorMessage: 'the worker crashed' });

      expect(
        await screen.findByText("the worker crashed. The lake's reason could not be loaded: HTTP 500"),
      ).not.toBeNull();
    });

    it('says so when the parked result is not the shape the page reads, rather than showing nothing', async () => {
      const { user, finish } = await renderPage({ success: false, status: 'refused', refusal: { code: 'data_corrupt' } });
      await user.click(runButton());

      finish('failed', { errorMessage: 'the worker crashed' });

      expect(await screen.findByText(/the worker crashed\. The lake's reason could not be loaded: /)).not.toBeNull();
    });
  });
});

describe('BatchRunnerComponent: the stock bars the lake lacks, before Run (#3524)', () => {
  /** QQQ lacks 12 of 60 sessions and AAPL 30; SPY and everything else is held. */
  const lacking: LakeReading = (symbol) => ({
    kind: 'ok',
    value: heldReport({ sessions: 60, missing: symbol === 'QQQ' ? 12 : symbol === 'AAPL' ? 30 : 0 }),
  });

  it('counts each member once for the entered window, in the split-adjusted tree, and says nothing when the lake holds it all', async () => {
    const { instrumentData } = await renderPage();

    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(3));

    for (const symbol of ['SPY', 'QQQ', 'AAPL']) {
      expect(instrumentData).toHaveBeenCalledWith({ symbol, from: '2025-09-02', to: '2025-12-01', tree: 'polygon_split_adjusted' });
    }
    expect(screen.queryByText(/in the lake yet/)).toBeNull();
    expect(screen.queryByRole('button', { name: /^Capture / })).toBeNull();
  });

  it('adds up the member-sessions the lake lacks and lists only the members that lack some, each with its own capture', async () => {
    await renderPage(undefined, lacking);

    expect(await screen.findByText(/42 member-sessions of stock bars aren’t in the lake yet, across 2 of 3 members\./)).toBeTruthy();
    expect(screen.getByText(/about one vendor call per member-session, so a cold universe is slow/)).toBeTruthy();
    expect(within(screen.getByTestId('holding-QQQ')).getByRole('button', { name: 'Capture 12 sessions' })).toBeTruthy();
    expect(within(screen.getByTestId('holding-AAPL')).getByRole('button', { name: 'Capture 30 sessions' })).toBeTruthy();
    expect(screen.queryByTestId('holding-SPY'), 'SPY is held in full').toBeNull();
  });

  it('starts no capture by itself and does not hold up Run study', async () => {
    const { captureRun, jobs, user } = await renderPage(undefined, lacking);
    await screen.findByText(/42 member-sessions/);

    expect(captureRun).not.toHaveBeenCalled();
    expect((runButton() as HTMLButtonElement).disabled).toBe(false);
    await user.click(runButton());
    expect(jobs.startJob).toHaveBeenCalledOnce();
  });

  it('captures the span the lake named for that member, through the shared runner, and counts that member again', async () => {
    const { captureRun, instrumentData, user } = await renderPage(undefined, lacking);
    let asked: unknown = null;
    captureRun.mockImplementation(async (select: (defaults: never) => unknown) => {
      asked = select(undefined as never);
      return { kind: 'completed', failure: null };
    });
    const counted = lacking('QQQ');
    await screen.findByText(/42 member-sessions/);

    await user.click(within(screen.getByTestId('holding-QQQ')).getByRole('button', { name: 'Capture 12 sessions' }));

    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(4));
    expect(asked).toEqual({
      symbol: 'QQQ', price_adjustment_mode: 'polygon_split_adjusted',
      start_trading_date_ms: counted.kind === 'ok' ? counted.value.fill_window?.start_trading_date_ms : undefined,
      end_trading_date_ms: counted.kind === 'ok' ? counted.value.fill_window?.end_trading_date_ms : undefined,
    });
    expect(instrumentData).toHaveBeenLastCalledWith(expect.objectContaining({ symbol: 'QQQ' }));
  });

  it('counts every member again once its own run ends, because the run filled the lake', async () => {
    const { instrumentData, user, finish } = await renderPage(undefined, lacking);
    await screen.findByText(/42 member-sessions/);
    await user.click(runButton());

    finish('completed');

    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(6));
  });

  it('says which count could not be read, and does not read it as nothing missing', async () => {
    await renderPage(undefined, (symbol) =>
      symbol === 'AAPL' ? { kind: 'unavailable', message: 'The data plane did not respond.' } : lakeHolds(symbol),
    );

    expect(await screen.findByText('Could not check which of this run’s sessions the lake holds.')).toBeTruthy();
    expect(within(screen.getByTestId('holding-AAPL')).getByText(/The data plane did not respond\./)).toBeTruthy();
    expect(screen.queryByText(/member-sessions of stock bars/)).toBeNull();
  });
});
