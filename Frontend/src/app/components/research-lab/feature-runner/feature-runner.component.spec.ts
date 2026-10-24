import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
  type TestRequest,
} from '@angular/common/http/testing';
import { signal, computed, type Signal } from '@angular/core';
import { vi } from 'vitest';

import { environment } from '../../../../environments/environment';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import {
  fakeVendorCatalog,
  provideFakeVendorCatalog,
} from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';

import { FeatureRunnerComponent } from './feature-runner.component';
import { JobsService, type JobState } from '../../../services/jobs.service';

/**
 * The feature runner moved from a synchronous `ResearchService` call to a
 * job-based SSE flow (`JobsService.startJob`). The component is now
 * mostly a thin orchestrator over `JobsService`, so the unit test focuses
 * on what the component actually owns: the `canRun` derived state and
 * the `runResearch()` → `JobsService.startJob` wiring. The full
 * SSE-driven completion flow is exercised end-to-end in the runner test
 * suite under `tests/jobs` and via manual smoke tests; recreating it
 * here would just be re-mocking `JobsService` internals.
 */

/** A one-indicator `/api/dataset/available` catalog. */
const CATALOG = {
  momentum: [
    {
      name: 'mom',
      category: 'momentum',
      description: 'Momentum.',
      configurable_params: [
        { name: 'length', type: 'int', default: 10, min: 1, max: 200, description: 'Lookback.' },
      ],
    },
  ],
};

interface JobsServiceMock {
  jobs: Signal<JobState[]>;
  job: (id: string) => JobState | undefined;
  startJob: ReturnType<typeof vi.fn>;
  cancelJob: ReturnType<typeof vi.fn>;
  fetchResult: ReturnType<typeof vi.fn>;
  dismiss: ReturnType<typeof vi.fn>;
}

describe('FeatureRunnerComponent', () => {
  let component: FeatureRunnerComponent;
  let fixture: ComponentFixture<FeatureRunnerComponent>;
  let jobsServiceMock: JobsServiceMock;
  // Backing signal so tests can simulate "this id is in a running job".
  let jobsByIdSignal: ReturnType<typeof signal<Map<string, JobState>>>;

  beforeEach(async () => {
    jobsByIdSignal = signal<Map<string, JobState>>(new Map());
    const jobsList = computed(() => Array.from(jobsByIdSignal().values()));

    jobsServiceMock = {
      jobs: jobsList,
      job: (id: string) => jobsByIdSignal().get(id),
      startJob: vi.fn().mockResolvedValue('job-id-1'),
      cancelJob: vi.fn().mockResolvedValue(undefined),
      fetchResult: vi.fn().mockResolvedValue({}),
      dismiss: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [
        FeatureRunnerComponent,
        HttpClientTestingModule,
      ],
      providers: [
        { provide: JobsService, useValue: jobsServiceMock },
        // The card reads the lake catalog on init; without this the real
        // service is constructed and fires a doomed XHR on every run.
        provideFakeTickerCatalog(
          fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' }]),
        ),
        // The joined picker universe also reads the vendor catalog; this spec
        // drives every request through HttpTestingController, so stub it to
        // keep `whenStable()` from hanging on an unflushed request.
        provideFakeVendorCatalog(fakeVendorCatalog([])),
        ...provideFakeDateWorld(),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(FeatureRunnerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('canRun is true with the default form values and no in-flight job', () => {
    // The component seeds AAPL / momentum_5m / 2024-01-01 / 2024-03-31
    // in its signals; that's enough to satisfy `canRun`.
    expect(component.canRun()).toBe(true);
  });

  it('canRun is false when the ticker is empty', () => {
    component.range.update((r) => ({ ...r, symbol: '' }));
    expect(component.canRun()).toBe(false);
  });

  it('canRun is false while a job is running', () => {
    component.jobId.set('job-id-1');
    jobsByIdSignal.set(
      new Map<string, JobState>([
        [
          'job-id-1',
          {
            id: 'job-id-1',
            type: 'feature_research',
            status: 'running',
            recentLogs: [],
            logSeq: 0,
          },
        ],
      ]),
    );

    expect(component.loading()).toBe(true);
    expect(component.canRun()).toBe(false);
  });

  it('runResearch dispatches the feature_research job via JobsService', async () => {
    component.range.set({
      symbol: 'aapl', // verify the upper-casing path
      from: '2024-01-01',
      to: '2024-03-31',
      resolution: 'minute',
      multiplier: 1,
    });
    // The catalog drives feature selection — pick the indicator + params
    // that map to the legacy feature_name='momentum_5m' worker call.
    component.selectedIndicator.set('mom');
    component.selectedParams.set({ length: 5 });

    await component.runResearch();

    expect(jobsServiceMock.startJob).toHaveBeenCalledOnce();
    const [type, payload] = jobsServiceMock.startJob.mock.calls[0];
    expect(type).toBe('feature_research');
    // Payload uses canonical TickerRequest field names — the wire adapter
    // emits symbol/from_date/to_date (not ticker/start_date/end_date).
    expect(payload).toMatchObject({
      symbol: 'AAPL',
      feature_name: 'momentum_5m',
      from_date: '2024-01-01',
      to_date: '2024-03-31',
      // The one session field, named: the study computes on extended hours (D02 is open) and says so.
      session: 'extended',
    });
    expect(component.jobId()).toBe('job-id-1');
  });

  it('offers only the bars the backend accepts, and no session choice the study would ignore', () => {
    const el = fixture.nativeElement as HTMLElement;

    const multipliers = Array.from(el.querySelectorAll('select[aria-label="Bar multiplier"] option')).map((o) => o.textContent?.trim());
    const resolutions = Array.from(el.querySelectorAll('[aria-label="Resolution"] button')).map((b) => b.textContent?.trim());

    expect(multipliers).toEqual(['1×', '3×', '5×', '15×']);
    expect(resolutions).toEqual(['1m']);
    expect(el.querySelector('[aria-label="Session"]')).toBeNull();
  });

  it('runResearch surfaces a startJob failure into the error signal', async () => {
    jobsServiceMock.startJob.mockRejectedValueOnce(new Error('Network error'));

    await component.runResearch();

    expect(component.error()).toBe('Network error');
    expect(component.jobId()).toBeNull();
  });

  const group = (over: Record<string, number>) => ({
    expected_sessions: 10, reused: 0, added: 10, missing: 0, failed: 0,
    building: 0, corrupt: 0, unavailable_from_provider: 0, ...over,
  });
  const dataSource = (coverage: unknown, over: Record<string, unknown> = {}) => ({
    kind: 'lake',
    provider: 'polygon',
    symbol: 'SPY',
    price_adjustment_mode: 'polygon_split_adjusted',
    session_policy: 'extended',
    window_start_ms: 1709528400000,
    window_end_ms: 1710478800000,
    effective_window: { start_ms: 1709528400000, end_ms: 1710478800000 },
    warmup_sessions: 0,
    bar_minutes: 5,
    aggregation: 'minute_fold_et_wall_clock_no_fill',
    identity_digest: 'def',
    calculation_version: 'feature-study/1',
    snapshot_digest: 'abc',
    coverage,
    bars_used: 1920,
    cache: { hit: false, cached_at_ms: null },
    ...over,
  });

  async function settleJob(status: 'completed' | 'failed', extra: Partial<JobState> = {}): Promise<HTMLElement> {
    component.jobId.set('job-id-1');
    jobsByIdSignal.set(
      new Map<string, JobState>([['job-id-1', { id: 'job-id-1', type: 'feature_research', status, recentLogs: [], logSeq: 0, ...extra }]]),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows the lake data-source line, with its added and reused counts, on a completed run', async () => {
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: true, ticker: 'SPY', feature_name: 'rsi_14', start_date: '2024-03-04', end_date: '2024-03-15',
      bars_used: 1920, error: null,
      mean_ic: 0.05, ic_t_stat: 1.2, ic_p_value: 0.2, nw_t_stat: 1.1, nw_p_value: 0.3, effective_n: 8,
      ic_values: [0.04, 0.06], ic_dates: ['2024-03-04', '2024-03-05'], adf_pvalue: 0.01, kpss_pvalue: 0.1,
      is_stationary: true, quantile_bins: [], is_monotonic: false, monotonicity_ratio: 0.5, robustness: null,
      feature_spec: null, validation_verdict: null, target: null, passed_validation: false,
      data_source: dataSource({
        window: group({ reused: 7, added: 3 }),
        warmup: group({ expected_sessions: 0, added: 0 }),
        artifacts: { fetched: 5, reused: 7, refreshed: 0 },
      }),
    });

    const el = await settleJob('completed');

    const line = el.querySelector('app-lake-study-source');
    expect(line?.textContent).toContain('Data lake, Polygon');
    expect(line?.textContent).toContain('5-minute bars, folded from 1-minute bars');
    expect(line?.textContent).toContain('3 added · 7 reused · 0 missing');
    expect(line?.textContent).not.toContain('No result');
  });

  it('shows why the lake refused a run and the counts it reached, not one sentence', async () => {
    const reason = 'The lake cannot admit this window: 1 of 10 sessions cannot be supplied by the provider: 2024-03-06 (provider_no_data).';
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: false, status: 'refused', ticker: 'SPY', error: reason,
      refusal: {
        code: 'data_unavailable_from_provider', reason,
        sessions: [{ group: 'window', trading_date_ms: 1709738400000, outcome: 'unavailable_from_provider', reason: 'provider_no_data' }],
        sessions_total: 1,
        coverage: {
          window: group({ added: 9, failed: 1, unavailable_from_provider: 1 }),
          warmup: group({ expected_sessions: 0, added: 0 }),
          artifacts: { fetched: 9, reused: 0, refreshed: 0 },
        },
      },
      data_source: dataSource({
        window: group({ added: 9, failed: 1, unavailable_from_provider: 1 }),
        warmup: group({ expected_sessions: 0, added: 0 }),
        artifacts: { fetched: 9, reused: 0, refreshed: 0 },
      }, { snapshot_digest: null, effective_window: null }),
    });

    const el = await settleJob('failed', { errorCode: 'data_unavailable_from_provider', errorMessage: reason });

    const text = el.textContent ?? '';
    expect(text).toContain('No result');
    expect(text).toContain('10 sessions · 0 reused · 9 added · 0 missing · 1 failed (1 unavailable from the provider)');
    expect(text).toContain('provider_no_data');
    expect(text).toContain('unavailable from the provider');
    expect(text.split(reason).length - 1, 'the reason is said once, not as an error banner as well').toBe(1);
    expect(el.querySelector('p-message')).toBeNull();
  });

  it('shows the failure message alone when the job failed on something other than a refused window', async () => {
    jobsServiceMock.fetchResult.mockRejectedValueOnce(new Error('404'));

    const el = await settleJob('failed', { errorCode: 'internal_error', errorMessage: 'The study crashed.' });

    expect(el.querySelector('p-message')?.textContent).toContain('The study crashed.');
    expect(el.querySelector('app-lake-study-source')).toBeNull();
  });

  describe('indicator catalog load', () => {
    const catalogUrl = `${environment.pythonServiceUrl}/api/dataset/available`;

    async function settleCatalog(respond: (req: TestRequest) => void): Promise<HTMLElement> {
      respond(TestBed.inject(HttpTestingController).expectOne(catalogUrl));
      await fixture.whenStable();
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }

    it('says so in the picker when the catalog fails to load', async () => {
      const el = await settleCatalog((req) =>
        req.flush('boom', { status: 500, statusText: 'Internal Server Error' }),
      );

      const picker = el.querySelector('app-indicator-picker');
      expect(picker?.querySelector('[role="alert"]')?.textContent).toContain(
        'Indicators could not be loaded.',
      );
      expect(picker?.textContent).not.toContain('No indicators available');
      // Fixed copy: the service's raw HTTP error never reaches the page.
      expect(picker?.textContent).not.toContain('Http failure response');
    });

    it('shows no failure when the catalog loads', async () => {
      const el = await settleCatalog((req) =>
        req.flush({ success: true, categories: CATALOG, total: 1 }),
      );

      const picker = el.querySelector('app-indicator-picker');
      expect(picker?.querySelector('[role="alert"]')).toBeNull();
      expect(picker?.textContent).not.toContain('Indicators could not be loaded.');
      expect(picker?.textContent).toContain('1 of 1');
    });
  });
});
