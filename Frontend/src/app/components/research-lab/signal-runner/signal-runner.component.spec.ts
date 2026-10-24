import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import {
  HttpClientTestingModule,
  HttpTestingController,
  type TestRequest,
} from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
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

import { JobsService, type JobState } from '../../../services/jobs.service';
import { RECORD_ID, storedRecord } from '../../../research/stored-study/testing/stored-study-fixtures';
import { SignalRunnerComponent } from './signal-runner.component';

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

describe('SignalRunnerComponent indicator catalog load', () => {
  const catalogUrl = `${environment.pythonServiceUrl}/api/dataset/available`;
  let fixture: ComponentFixture<SignalRunnerComponent>;
  let startJob: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    const jobs = signal<JobState[]>([]);
    startJob = vi.fn().mockResolvedValue('job-id-1');
    await TestBed.configureTestingModule({
      imports: [SignalRunnerComponent, HttpClientTestingModule],
      providers: [
        {
          provide: JobsService,
          useValue: {
            jobs,
            job: () => undefined,
            startJob,
            cancelJob: vi.fn(),
            fetchResult: vi.fn(),
            dismiss: vi.fn(),
          },
        },
        provideRouter([]),
        // The ticker card reads the lake catalog on init; without this the
        // real service fires a doomed XHR on every run.
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

    fixture = TestBed.createComponent(SignalRunnerComponent);
    fixture.detectChanges();
  });

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

  describe('rerun from a saved run', () => {
    it('fills the inputs, flip sign and regime gate from the saved run, starts nothing, then names it when Run study is pressed', async () => {
      const component = fixture.componentInstance;
      fixture.componentRef.setInput('rerun', RECORD_ID);
      fixture.detectChanges();
      TestBed.inject(HttpTestingController)
        .expectOne(`${environment.pythonServiceUrl}/api/research/study-records/signal-study/${RECORD_ID}`)
        .flush(storedRecord('signal-study'));
      await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
      await fixture.whenStable();

      expect(component.range()).toEqual({ symbol: 'SPY', from: '2024-01-02', to: '2024-03-28', resolution: 'minute', multiplier: 15 });
      expect(component.selectedIndicator()).toBe('macd');
      expect(component.selectedParams()).toEqual({ fast: 12, slow: 26, signal: 9 });
      expect(component.flipSign()).toBe(false);
      expect(component.regimeGateEnabled()).toBe(true);
      expect(startJob).not.toHaveBeenCalled();

      await component.runSignalEngine();

      expect(startJob.mock.calls[0][1]).toMatchObject({
        feature_name: 'macd_signal',
        flip_sign: false,
        rerun_of: RECORD_ID,
        session: 'extended', // the bars it reads, not the "rth" the wire would fill in
      });
    });
  });

  describe('study layout', () => {
    const root = () => fixture.nativeElement as HTMLElement;

    it('offers only the bars the 15-bar target can use, and no session control', () => {
      const resolutions = Array.from(root().querySelectorAll('.res-btn')).map((el) => el.textContent?.trim());
      const multipliers = Array.from(
        root().querySelectorAll('select[aria-label="Bar multiplier"] option'),
      ).map((el) => el.textContent?.trim());

      expect.soft(resolutions, 'hour and daily bars always fail').toEqual(['1m']);
      expect.soft(multipliers, 'bars of 30 minutes or longer always fail').toEqual(['1×', '5×', '15×']);
      expect.soft(root().querySelector('.session'), 'the session is not a control').toBeNull();
      expect.soft(root().querySelector('[data-testid="study-constraints"]')?.textContent).toContain(
        'extended hours included, fixed',
      );
      // The default window is long enough for the picker's hour-bars hint.
      expect.soft(root().textContent, 'no hint to switch to bars that always fail').not.toContain('Switch to hour');
    });

    it('starts nothing when an input changes', () => {
      const component = fixture.componentInstance;
      component.range.update((r) => ({ ...r, symbol: 'MSFT' }));
      component.flipSign.set(false);
      component.regimeGateEnabled.set(false);
      fixture.detectChanges();

      expect(startJob).not.toHaveBeenCalled();
    });

    it('starts one job when Run study is pressed twice before the page re-renders', async () => {
      const run = root().querySelector<HTMLButtonElement>('button.study-run');
      if (run === null) throw new Error('no Run study button');

      run.click();
      run.click();
      await fixture.whenStable();

      expect(startJob).toHaveBeenCalledTimes(1);
    });
  });
});

describe('SignalRunnerComponent job wiring', () => {
  let component: SignalRunnerComponent;
  let fixture: ComponentFixture<SignalRunnerComponent>;
  let jobsById: ReturnType<typeof signal<Map<string, JobState>>>;
  let jobsServiceMock: {
    jobs: ReturnType<typeof computed<JobState[]>>;
    job: (id: string) => JobState | undefined;
    startJob: ReturnType<typeof vi.fn>;
    cancelJob: ReturnType<typeof vi.fn>;
    fetchResult: ReturnType<typeof vi.fn>;
    dismiss: ReturnType<typeof vi.fn>;
  };

  const group = (over: Record<string, number>) => ({
    expected_sessions: 82, reused: 0, added: 82, missing: 0, failed: 0,
    building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 0, ...over,
  });

  beforeEach(async () => {
    jobsById = signal<Map<string, JobState>>(new Map());
    jobsServiceMock = {
      jobs: computed(() => Array.from(jobsById().values())),
      job: (id: string) => jobsById().get(id),
      startJob: vi.fn().mockResolvedValue('job-id-1'),
      cancelJob: vi.fn().mockResolvedValue(undefined),
      fetchResult: vi.fn().mockResolvedValue({}),
      dismiss: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [SignalRunnerComponent, HttpClientTestingModule],
      providers: [
        { provide: JobsService, useValue: jobsServiceMock },
        provideFakeTickerCatalog(
          fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' }]),
        ),
        provideFakeVendorCatalog(fakeVendorCatalog([])),
        provideRouter([]),
        ...provideFakeDateWorld(),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SignalRunnerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  function setJob(status: JobState['status'], extra: Partial<JobState> = {}): void {
    component.jobId.set('job-id-1');
    jobsById.set(
      new Map<string, JobState>([
        ['job-id-1', { id: 'job-id-1', type: 'signal_engine', status, recentLogs: [], logSeq: 0, ...extra }],
      ]),
    );
  }

  it('sends the body the backend model is pinned against: the one session field, named', async () => {
    await component.runSignalEngine();

    const [type, payload] = jobsServiceMock.startJob.mock.calls[0];
    expect(type).toBe('signal_engine');
    // tests/jobs/test_signal_study_job.py parses exactly this body. The session is named, not defaulted: the study
    // computes on extended hours until the owner decides D02, and the request carries no second session field.
    expect(payload).toEqual({
      symbol: 'AAPL',
      from_date: '2024-01-02',
      to_date: '2024-06-28',
      timespan: 'minute',
      multiplier: 15,
      session: 'extended',
      feature_name: 'rsi_14',
      flip_sign: true,
      regime_gate_enabled: true,
      force: false,
    });
  });

  const dataSource = (coverage: unknown, over: Record<string, unknown> = {}) => ({
    kind: 'lake',
    provider: 'polygon',
    symbol: 'AAPL',
    price_adjustment_mode: 'polygon_split_adjusted',
    session_policy: 'extended',
    window_start_ms: 1704171600000,
    window_end_ms: 1719550800000,
    effective_window: { start_ms: 1704171600000, end_ms: 1719550800000 },
    warmup_sessions: 0,
    bar_minutes: 15,
    aggregation: 'minute_fold_et_wall_clock_no_fill',
    identity_digest: 'def',
    calculation_version: 'signal-study/1',
    snapshot_digest: 'abc',
    look_forward_bars: 15,
    coverage,
    bars_used: 8000,
    cache: { hit: false, cached_at_ms: null },
    ...over,
  });

  const lines = (el: HTMLElement) => el.querySelectorAll('app-workspace-data-source');

  async function settleJob(status: 'completed' | 'failed', extra: Partial<JobState> = {}): Promise<HTMLElement> {
    setJob(status, extra);
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

  it('shows the lake data-source line, with its added and reused counts and its window as dates, on a completed run', async () => {
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: true,
      ticker: 'AAPL',
      feature_name: 'rsi_14',
      start_date: '2024-01-02',
      end_date: '2024-06-28',
      bars_used: 8000,
      data_source: dataSource({
        window: group({ reused: 70, added: 12 }),
        warmup: group({ expected_sessions: 0, added: 0 }),
        artifacts: { fetched: 14, reused: 70, refreshed: 0 },
      }),
      error: null,
    });

    const el = await settleJob('completed');

    expect(lines(el), 'one data-source line, the layout\'s').toHaveLength(1);
    const line = lines(el)[0].textContent ?? '';
    expect(line).toContain('Data lake, Polygon');
    expect(line).toContain('15-minute bars, folded from 1-minute bars');
    expect(line).toContain('12 added · 70 reused · 0 missing');
    expect(line, 'a window of dates carries no time of day').not.toMatch(/\d{1,2}:\d{2}/);
    expect(el.textContent).not.toContain('No result');
    expect(component.result()?.dataSource?.cache).toEqual({ hit: false, cachedAtMs: null });
  });

  it('shows why the lake refused a run and the counts it reached, not one sentence', async () => {
    const reason = 'The lake cannot admit this window: 1 of 82 sessions cannot be supplied by the provider: 2024-03-06 (provider_no_data).';
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: false, status: 'refused', ticker: 'AAPL', error: reason,
      refusal: {
        code: 'data_unavailable_from_provider', reason,
        sessions: [{ group: 'window', trading_date_ms: 1709738400000, outcome: 'unavailable_from_provider', reason: 'provider_no_data', failure_reason: 'provider_no_data' }],
        sessions_total: 1,
        coverage: {
          window: group({ added: 81, failed: 1, unavailable_from_provider: 1 }),
          warmup: group({ expected_sessions: 0, added: 0 }),
          artifacts: { fetched: 81, reused: 0, refreshed: 0 },
        },
      },
      data_source: dataSource({
        window: group({ added: 81, failed: 1, unavailable_from_provider: 1 }),
        warmup: group({ expected_sessions: 0, added: 0 }),
        artifacts: { fetched: 81, reused: 0, refreshed: 0 },
      }, { snapshot_digest: null, effective_window: null }),
    });

    const el = await settleJob('failed', { errorCode: 'data_unavailable_from_provider', errorMessage: reason });

    const text = el.textContent ?? '';
    expect(text).toContain('No result');
    expect(text).toContain('82 sessions · 0 reused · 81 added · 0 missing · 1 failed (1 unavailable from the provider)');
    expect(text).toContain('provider_no_data');
    expect(text.split(reason).length - 1, 'the reason is said once, not as an error banner as well').toBe(1);
    expect(el.querySelector('p-message')).toBeNull();
    expect(lines(el), 'the refused source is the layout\'s one line').toHaveLength(1);
    expect(lines(el)[0].textContent).toContain('Data lake, Polygon');
  });

  it('shows the reason of a refusal that came before anything was read, with no counts', async () => {
    const reason = '15-minute bars are too long for the 15-bar forward target under regular-hours sessions: the longest session in this window holds at most 26 of them.';
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: false, status: 'refused', ticker: 'AAPL', error: reason,
      refusal: { code: 'resolution_unsupported', reason, sessions: [], sessions_total: 0, coverage: null },
      data_source: null,
    });

    const el = await settleJob('failed', { errorCode: 'resolution_unsupported', errorMessage: reason });

    const text = el.textContent ?? '';
    expect(text).toContain('No result');
    expect(text).toContain(reason);
    expect(text, 'nothing was admitted, so no counts are claimed').not.toContain('sessions ·');
    expect(el.querySelector('p-message')).toBeNull();
  });

  it('shows the failure message alone when the job failed on something other than a refused window', async () => {
    jobsServiceMock.fetchResult.mockRejectedValueOnce(new HttpErrorResponse({ status: 404, statusText: 'Not Found' }));

    const el = await settleJob('failed', { errorCode: 'ValueError', errorMessage: 'Forward return series failed validation' });

    const shown = el.querySelector('p-message')?.textContent ?? '';
    expect(shown).toContain('Forward return series failed validation');
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
