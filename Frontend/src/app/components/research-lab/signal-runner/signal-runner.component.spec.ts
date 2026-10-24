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

  it('sends the body the backend model is pinned against: the legacy rth label and no session policy', async () => {
    await component.runSignalEngine();

    const [type, payload] = jobsServiceMock.startJob.mock.calls[0];
    expect(type).toBe('signal_engine');
    // tests/jobs/test_signal_study_job.py parses exactly this body: `session` stays the inert legacy label, and the
    // session policy is left to the backend default (extended hours) until the owner decides D02.
    expect(payload).toEqual({
      symbol: 'AAPL',
      from_date: '2024-01-02',
      to_date: '2024-06-28',
      timespan: 'minute',
      multiplier: 15,
      session: 'rth',
      feature_name: 'rsi_14',
      flip_sign: true,
      regime_gate_enabled: true,
      force: false,
    });
  });

  it('carries the lake data source and its coverage counts from a completed run into the result', async () => {
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: true,
      ticker: 'AAPL',
      feature_name: 'rsi_14',
      start_date: '2024-01-02',
      end_date: '2024-06-28',
      bars_used: 8000,
      data_source: {
        kind: 'lake',
        provider: 'polygon',
        price_adjustment_mode: 'polygon_split_adjusted',
        bar_minutes: 15,
        session_policy: 'extended',
        window_start_ms: 1704171600000,
        window_end_ms: 1719550800000,
        warmup_sessions: 0,
        look_forward_bars: 15,
        snapshot_digest: 'abc',
        identity_digest: 'def',
        calculation_version: 'signal-study/1',
        coverage: {
          window: group({ reused: 70, added: 12 }),
          warmup: group({ expected_sessions: 0, added: 0 }),
          artifacts: { fetched: 14, reused: 70, refreshed: 0 },
        },
        bars_used: 8000,
        cache: { hit: false, cached_at_ms: null },
      },
      error: null,
    });
    setJob('completed');

    fixture.detectChanges();
    await fixture.whenStable();

    const source = component.result()?.dataSource;
    expect(source?.sessionPolicy).toBe('extended');
    expect(source?.lookForwardBars).toBe(15);
    expect(source?.coverage?.window).toMatchObject({ expectedSessions: 82, reused: 70, added: 12, unavailableFromProvider: 0 });
    expect(source?.coverage?.artifacts).toEqual({ fetched: 14, reused: 70, refreshed: 0 });
    expect(source?.cache).toEqual({ hit: false, cachedAtMs: null });
  });

  it('maps a stored report that has no data source without one', async () => {
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: true, ticker: 'AAPL', feature_name: 'rsi_14', start_date: '2024-01-02', end_date: '2024-06-28',
      bars_used: 8000, error: null,
    });
    setJob('completed');

    fixture.detectChanges();
    await fixture.whenStable();

    expect(component.result()?.dataSource).toBeNull();
  });

  it('shows the typed reason a refused run carries instead of an opaque failure', () => {
    setJob('failed', {
      errorMessage: '60-minute bars are too long for the 15-bar forward target: a day holds at most 24 of them.',
    });

    fixture.detectChanges();

    expect(component.error()).toBe(
      '60-minute bars are too long for the 15-bar forward target: a day holds at most 24 of them.',
    );
  });
});
