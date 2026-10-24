import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
  type TestRequest,
} from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
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

import { JobsService, type JobState } from '../../../services/jobs.service';
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

  beforeEach(async () => {
    const jobs = signal<JobState[]>([]);
    await TestBed.configureTestingModule({
      imports: [SignalRunnerComponent, HttpClientTestingModule],
      providers: [
        {
          provide: JobsService,
          useValue: {
            jobs,
            job: () => undefined,
            startJob: vi.fn(),
            cancelJob: vi.fn(),
            fetchResult: vi.fn(),
            dismiss: vi.fn(),
          },
        },
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
    building: 0, corrupt: 0, unavailable_from_provider: 0, ...over,
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

  it('offers only the bars the backend accepts, and no session choice the study would ignore', () => {
    const el = fixture.nativeElement as HTMLElement;

    const multipliers = Array.from(el.querySelectorAll('select[aria-label="Bar multiplier"] option')).map((o) => o.textContent?.trim());
    const resolutions = Array.from(el.querySelectorAll('[aria-label="Resolution"] button')).map((b) => b.textContent?.trim());

    expect(multipliers).toEqual(['1×', '5×', '15×']);
    expect(resolutions).toEqual(['1m']);
    expect(el.querySelector('[aria-label="Session"]')).toBeNull();
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

  async function settleJob(status: 'completed' | 'failed', extra: Partial<JobState> = {}): Promise<HTMLElement> {
    setJob(status, extra);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows the lake data-source line, with its added and reused counts, on a completed run', async () => {
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

    const line = el.querySelector('app-lake-study-source');
    expect(line?.textContent).toContain('Data lake, Polygon');
    expect(line?.textContent).toContain('15-minute bars, folded from 1-minute bars');
    expect(line?.textContent).toContain('12 added · 70 reused · 0 missing');
    expect(line?.textContent).not.toContain('No result');
    expect(component.result()?.dataSource?.cache).toEqual({ hit: false, cachedAtMs: null });
  });

  it('says the source was not recorded on a stored report that has none, and does not claim the lake', async () => {
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: true, ticker: 'AAPL', feature_name: 'rsi_14', start_date: '2024-01-02', end_date: '2024-06-28',
      bars_used: 8000, error: null,
    });

    const el = await settleJob('completed');

    expect(component.result()?.dataSource).toBeNull();
    expect(el.querySelector('app-lake-study-source')?.textContent).not.toContain('Data lake');
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
  });

  it('shows the reason of a refusal that came before anything was read, with no source line', async () => {
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
    expect(text).not.toContain('Data lake');
    expect(el.querySelector('p-message')).toBeNull();
  });

  it('shows the failure message alone when the job failed on something other than a refused window', async () => {
    jobsServiceMock.fetchResult.mockRejectedValueOnce(new Error('404'));

    const el = await settleJob('failed', { errorCode: 'ValueError', errorMessage: 'Forward return series failed validation' });

    expect(el.querySelector('p-message')?.textContent).toContain('Forward return series failed validation');
    expect(el.querySelector('app-lake-study-source')).toBeNull();
  });
});
