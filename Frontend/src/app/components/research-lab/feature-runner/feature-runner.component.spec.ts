import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import {
  HttpClientTestingModule,
  HttpTestingController,
  type TestRequest,
} from '@angular/common/http/testing';
import { signal, computed, type Signal } from '@angular/core';
import { vi } from 'vitest';
import { provideRouter } from '@angular/router';

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
import { FEATURE_INPUTS, RECORD_ID, storedRecord } from '../../../research/stored-study/testing/stored-study-fixtures';

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
        provideRouter([]),
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
      // The bars it reads include extended hours; a saved run must not say "rth".
      session: 'extended',
    });
    expect(component.jobId()).toBe('job-id-1');
  });

  describe('study layout', () => {
    const root = () => fixture.nativeElement as HTMLElement;
    const runButton = () => root().querySelector<HTMLButtonElement>('button.study-run');

    it('offers only the bars the 15-minute look-ahead can use, and no session control', () => {
      const resolutions = Array.from(root().querySelectorAll('.res-btn')).map((el) => el.textContent?.trim());
      const multipliers = Array.from(
        root().querySelectorAll('select[aria-label="Bar multiplier"] option'),
      ).map((el) => el.textContent?.trim());

      expect.soft(resolutions, 'hour and daily bars always fail').toEqual(['1m']);
      expect.soft(multipliers, '×60 and ×240 bars always fail').toEqual(['1×', '5×', '15×']);
      expect.soft(root().querySelector('.session'), 'the session is not a control').toBeNull();
      expect.soft(root().querySelector('[data-testid="study-constraints"]')?.textContent).toContain(
        'extended hours included, fixed',
      );
    });

    it('starts nothing when an input changes', () => {
      component.range.update((r) => ({ ...r, symbol: 'MSFT', from: '2024-02-01' }));
      component.selectedIndicator.set('mom');
      component.selectedParams.set({ length: 5 });
      fixture.detectChanges();

      expect(jobsServiceMock.startJob).not.toHaveBeenCalled();
    });

    it('starts one job when Run study is pressed twice before the page re-renders', async () => {
      const run = runButton();
      if (run === null) throw new Error('no Run study button');

      run.click();
      run.click();
      await fixture.whenStable();

      expect(jobsServiceMock.startJob).toHaveBeenCalledTimes(1);
    });

    it('leaves a running job running when the study is left', () => {
      component.jobId.set('job-id-1');
      jobsByIdSignal.set(
        new Map<string, JobState>([
          ['job-id-1', { id: 'job-id-1', type: 'feature_research', status: 'running', recentLogs: [], logSeq: 0 }],
        ]),
      );
      fixture.detectChanges();

      fixture.destroy();

      expect(jobsServiceMock.cancelJob).not.toHaveBeenCalled();
    });
  });

  describe('rerun from a saved run', () => {
    const recordUrl = `${environment.pythonServiceUrl}/api/research/study-records/feature-study/${RECORD_ID}`;

    async function openSaved(answer: ReturnType<typeof storedRecord> | { refuse: number }): Promise<void> {
      fixture.componentRef.setInput('rerun', RECORD_ID);
      fixture.detectChanges();
      const request = TestBed.inject(HttpTestingController).expectOne(recordUrl);
      if ('refuse' in answer) request.flush({}, { status: answer.refuse, statusText: 'refused' });
      else request.flush(answer);
      await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
      await fixture.whenStable();
      fixture.detectChanges();
    }

    const notice = () => (fixture.nativeElement as HTMLElement).querySelector('[data-testid="study-prefill"]');

    it('fills the inputs from the saved run, says so, and starts nothing', async () => {
      await openSaved(storedRecord('feature-study'));

      expect(component.range()).toEqual({
        symbol: 'SPY',
        from: '2024-01-02',
        to: '2024-03-28',
        resolution: 'minute',
        multiplier: 5,
      });
      expect(component.selectedIndicator()).toBe('rsi');
      expect(component.selectedParams()).toEqual({ length: 14 });
      expect(notice()?.textContent).toContain('Nothing has run');
      expect(jobsServiceMock.startJob).not.toHaveBeenCalled();
    });

    it('names the saved run as the new run’s parent when Run study is pressed, once', async () => {
      await openSaved(storedRecord('feature-study'));

      await component.runResearch();
      await component.runResearch();

      const [first, second] = jobsServiceMock.startJob.mock.calls.map(([, payload]) => payload);
      expect(first).toMatchObject({ symbol: 'SPY', feature_name: 'rsi_14', rerun_of: RECORD_ID });
      expect(second).not.toHaveProperty('rerun_of');
      fixture.detectChanges();
      expect(notice(), 'the fill has done its job once a run started').toBeNull();
    });

    it('keeps the lineage for the next press when the run did not start', async () => {
      await openSaved(storedRecord('feature-study'));
      jobsServiceMock.startJob.mockRejectedValueOnce(new Error('Network error'));

      await component.runResearch();
      await component.runResearch();

      expect(jobsServiceMock.startJob.mock.calls.map(([, payload]) => payload.rerun_of)).toEqual([RECORD_ID, RECORD_ID]);
    });

    it('leaves the inputs as they were and says so when the saved run is not found', async () => {
      const before = component.range();

      await openSaved({ refuse: 404 });

      expect(component.range()).toBe(before);
      expect(notice()?.textContent).toContain('was not found, so nothing was filled in');
      await component.runResearch();
      expect(jobsServiceMock.startJob.mock.calls[0][1]).not.toHaveProperty('rerun_of');
    });

    it('fills nothing when the saved run’s inputs are not ones this page offers', async () => {
      const before = component.range();
      const hourly = storedRecord('feature-study', { inputs: { ...FEATURE_INPUTS, timespan: 'hour' } as never });

      await openSaved(hourly);

      expect(component.range()).toBe(before);
      expect(notice()?.textContent).toContain('could not be read');
    });
  });

  it('runResearch surfaces a startJob failure into the error signal', async () => {
    jobsServiceMock.startJob.mockRejectedValueOnce(new Error('Network error'));

    await component.runResearch();

    expect(component.error()).toBe('Network error');
    expect(component.jobId()).toBeNull();
  });

  describe('lake data source', () => {
    const group = (over: Record<string, number>) => ({
      expected_sessions: 10, reused: 0, added: 10, missing: 0, failed: 0,
      building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 0, ...over,
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
    const lines = (el: HTMLElement) => el.querySelectorAll('app-workspace-data-source');

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

    it('names the lake before anything has run, not a provider fetch', () => {
      const el = fixture.nativeElement as HTMLElement;

      expect(lines(el)).toHaveLength(1);
      expect(lines(el)[0].textContent).toContain('Data lake');
    });

    it('shows the result\'s own lake source, with its added and reused counts and its window as dates, on a completed run', async () => {
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

      expect(lines(el), 'one data-source line, the layout\'s').toHaveLength(1);
      const line = lines(el)[0].textContent ?? '';
      expect(line).toContain('Data lake, Polygon');
      expect(line).toContain('5-minute bars, folded from 1-minute bars');
      expect(line).toContain('3 added · 7 reused · 0 missing');
      expect(line, 'a window of dates carries no time of day').not.toMatch(/\d{1,2}:\d{2}/);
    });

    it('shows why the lake refused a run and the counts it reached, not one sentence', async () => {
      const reason = 'The lake cannot admit this window: 1 of 10 sessions cannot be supplied by the provider: 2024-03-06 (provider_no_data).';
      jobsServiceMock.fetchResult.mockResolvedValueOnce({
        success: false, status: 'refused', ticker: 'SPY', error: reason,
        refusal: {
          code: 'data_unavailable_from_provider', reason,
          sessions: [{ group: 'window', trading_date_ms: 1709738400000, outcome: 'unavailable_from_provider', reason: 'provider_no_data', failure_reason: 'provider_no_data' }],
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
      expect(lines(el), 'the refused source is the layout\'s one line').toHaveLength(1);
      expect(lines(el)[0].textContent).toContain('Data lake, Polygon');
    });

    it('shows the failure message alone when the job failed on something other than a refused window', async () => {
      jobsServiceMock.fetchResult.mockRejectedValueOnce(new HttpErrorResponse({ status: 404, statusText: 'Not Found' }));

      const el = await settleJob('failed', { errorCode: 'internal_error', errorMessage: 'The study crashed.' });

      const shown = el.querySelector('p-message')?.textContent ?? '';
      expect(shown).toContain('The study crashed.');
      expect(shown, 'a missing result is the expected answer, not a second failure').not.toContain('could not be loaded');
      expect(el.querySelector('app-lake-study-source')).toBeNull();
    });

    it('says so when the failed job\'s result could not be read for any reason other than there being none', async () => {
      jobsServiceMock.fetchResult.mockRejectedValueOnce(
        new HttpErrorResponse({ status: 500, statusText: 'Internal Server Error', url: '/api/jobs/job-id-1/result' }),
      );

      const el = await settleJob('failed', { errorCode: 'data_corrupt', errorMessage: 'The lake cannot admit this window.' });

      const shown = el.querySelector('p-message')?.textContent ?? '';
      expect(shown).toContain('The lake cannot admit this window.');
      expect(shown).toContain("The lake's reason could not be loaded");
      expect(shown).toContain('500');
    });

    it('says so when the parked refusal is not the shape the page reads, rather than showing nothing', async () => {
      jobsServiceMock.fetchResult.mockResolvedValueOnce({ success: false, status: 'refused', refusal: { code: 'data_corrupt' } });

      const el = await settleJob('failed', { errorCode: 'data_corrupt', errorMessage: 'The lake cannot admit this window.' });

      expect(el.querySelector('p-message')?.textContent).toContain("The lake's reason could not be loaded");
    });
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
