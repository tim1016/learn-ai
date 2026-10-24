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
import { screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';

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
import {
  RECORD_ID,
  SIGNAL_INPUTS,
  SIGNAL_RESULT,
  storedRecord,
} from '../../../research/stored-study/testing/stored-study-fixtures';
import { DataLakeService, type DataLakeRead, type InstrumentDataReport } from '../../../shared/data-lake';
import type { BackfillJobRun } from '../../../shared/data-lake/backfill-job-runner';
import { TradeBackfillRunner, type TradeBackfillEnd } from '../../../shared/data-lake/trade-backfill-runner';
import type { TradeBackfillWindow } from '../../../shared/data-lake/trade-backfill-spec';
import { heldReport } from '../../../shared/data-lake/testing/instrument-data-report';
import { SignalRunnerComponent } from './signal-runner.component';
import signalBarMinutes from '@repo-contracts/signal-study-bar-minutes.json';

/** The sessions in the page's default window, as the lake's count of them reports. */
const SESSIONS = 124;

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
  // The lake's count of the sessions the window needs (the page reads it once per window), and the shared capture.
  let instrumentData: ReturnType<typeof vi.fn<(query: unknown) => Promise<DataLakeRead<InstrumentDataReport>>>>;
  let captureRun: ReturnType<typeof vi.fn<(select: (defaults: never) => TradeBackfillWindow, accepted: (job: BackfillJobRun) => boolean | Promise<boolean>) => Promise<TradeBackfillEnd>>>;

  beforeEach(async () => {
    const jobs = signal<JobState[]>([]);
    startJob = vi.fn().mockResolvedValue('job-id-1');
    // Left alone, the lake holds every session the default window needs, so only the specs about the count see a warning.
    instrumentData = vi.fn().mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: SESSIONS }) });
    captureRun = vi.fn().mockResolvedValue({ kind: 'completed', failure: null });
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
        { provide: DataLakeService, useValue: { instrumentData } },
        { provide: TradeBackfillRunner, useValue: { run: captureRun } },
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
    const recordUrl = `${environment.pythonServiceUrl}/api/research/study-records/signal-study/${RECORD_ID}`;

    async function openSaved(answer: ReturnType<typeof storedRecord>): Promise<void> {
      fixture.componentRef.setInput('rerun', RECORD_ID);
      fixture.detectChanges();
      TestBed.inject(HttpTestingController).expectOne(recordUrl).flush(answer);
      await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
      await fixture.whenStable();
      fixture.detectChanges();
    }

    const notice = () => (fixture.nativeElement as HTMLElement).querySelector('[data-testid="study-prefill"]');

    it('fills the inputs, flip sign and regime gate from the saved run, starts nothing, then names it when Run study is pressed', async () => {
      const component = fixture.componentInstance;
      await openSaved(storedRecord('signal-study'));

      expect(component.range()).toEqual({ symbol: 'SPY', from: '2024-01-02', to: '2024-03-28', resolution: 'minute', multiplier: 15, session: 'extended' });
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
        session: 'extended',
      });
    });

    it.each([
      ['regular hours', { session: 'rth', multiplier: 5 }, 'rth', 5, 'RTH'],
      ['extended hours', { session: 'extended', multiplier: 10 }, 'extended', 10, 'Ext'],
    ])('fills the session and the bars the saved run computed on: %s', async (_name, over, session, multiplier, pressed) => {
      await openSaved(storedRecord('signal-study', { inputs: { ...SIGNAL_INPUTS, ...over } as never }));

      expect(fixture.componentInstance.range()).toMatchObject({ session, multiplier });
      expect(screen.getByRole('button', { name: pressed }).getAttribute('aria-pressed')).toBe('true');
      expect(fixture.nativeElement.querySelector('select[aria-label="Bar multiplier"]').value).toBe(String(multiplier));
    });

    it('fills extended hours for a saved run that names no session, which is what the study computed on before it did', async () => {
      const unnamed = Object.fromEntries(Object.entries(SIGNAL_INPUTS).filter(([key]) => key !== 'session'));

      await openSaved(storedRecord('signal-study', { inputs: unnamed as never }));

      expect(fixture.componentInstance.range().session).toBe('extended');
    });

    it('fills nothing when the saved run’s session is not one this page offers', async () => {
      const before = fixture.componentInstance.range();

      await openSaved(storedRecord('signal-study', { inputs: { ...SIGNAL_INPUTS, session: 'overnight' } as never }));

      expect(fixture.componentInstance.range()).toBe(before);
      expect(notice()?.textContent).toContain('could not be read');
    });

    it('fills nothing, and names the session, when the saved run’s bars are not ones that session offers', async () => {
      const before = fixture.componentInstance.range();

      await openSaved(storedRecord('signal-study', { inputs: { ...SIGNAL_INPUTS, session: 'rth', multiplier: 15 } as never }));

      expect(fixture.componentInstance.range()).toBe(before);
      expect(notice()?.textContent).toContain('15-minute bars are not offered here for regular hours');
    });
  });

  describe('study layout', () => {
    const root = () => fixture.nativeElement as HTMLElement;

    const barLengths = () =>
      Array.from(root().querySelectorAll('select[aria-label="Bar multiplier"] option')).map((el) => el.textContent?.trim());
    const barSelect = () => root().querySelector<HTMLSelectElement>('select[aria-label="Bar multiplier"]');
    const barNote = () => root().querySelector('[data-testid="bar-size-note"]')?.textContent?.replace(/\s+/g, ' ').trim();

    /** Presses a button of the page and lets the page settle. */
    async function press(name: string): Promise<void> {
      await userEvent.setup().click(screen.getByRole('button', { name }));
      await fixture.whenStable();
      fixture.detectChanges();
    }

    it('offers only minute bars, and under the default extended hours the lengths the server accepts for them', () => {
      const resolutions = Array.from(root().querySelectorAll('.res-btn')).map((el) => el.textContent?.trim());

      expect.soft(resolutions, 'hour and daily bars always fail').toEqual(['1m']);
      expect.soft(barLengths(), 'the lengths that divide the hour and reach the 70% gate over an extended session').toEqual(
        ['1×', '2×', '3×', '4×', '5×', '6×', '10×', '12×', '15×'],
      );
      expect.soft(barSelect()?.value, 'the bars the study has always read').toBe('15');
      expect.soft(root().querySelector('[data-testid="study-constraints"]')?.textContent).not.toContain('extended hours included');
      expect.soft(root().querySelector('[data-testid="study-constraints"]')?.textContent).toContain('15 bars');
      expect.soft(barNote(), 'extended hours need no explanation of their list').toBeUndefined();
      // The default window is long enough for the picker's hour-bars hint.
      expect.soft(root().textContent, 'no hint to switch to bars that always fail').not.toContain('Switch to hour');
    });

    it('offers regular and extended hours as buttons to press, extended chosen, and does not fix the session', () => {
      const rth = screen.getByRole('button', { name: 'RTH' }) as HTMLButtonElement;
      const ext = screen.getByRole('button', { name: 'Ext' }) as HTMLButtonElement;

      expect.soft(rth.disabled).toBe(false);
      expect.soft(ext.disabled).toBe(false);
      expect.soft(ext.getAttribute('aria-pressed'), 'extended is what the study has always read').toBe('true');
      expect.soft(rth.getAttribute('aria-pressed')).toBe('false');
    });

    it('sends the session that was pressed, and the bars that session offers, and extended with the entered bars when nothing was pressed', async () => {
      await press('Run study');
      await press('RTH');
      await press('Run study');
      await press('Ext');
      await press('Run study');

      expect(startJob.mock.calls.map(([, payload]) => [payload.session, payload.multiplier])).toEqual([
        ['extended', 15],
        ['rth', 6],
        ['extended', 15],
      ]);
    });

    it('offers under regular hours only the lengths the server accepts for them, says why, and shows the length the run uses', async () => {
      await press('RTH');

      expect.soft(barLengths()).toEqual(['1×', '2×', '3×', '4×', '5×', '6×']);
      expect.soft(barSelect()?.value, '15 minutes is not offered here, so the nearest length is shown').toBe('6');
      expect.soft(barNote()).toContain('15-minute bars are not offered for this session, so this run uses 6-minute bars.');
      expect.soft(barNote()).toContain('Regular hours offer bars of 1, 2, 3, 4, 5, 6 minutes only');
      expect.soft(barNote()).toContain('needs 70% of its bars to have a target');

      await press('Ext');

      expect.soft(barLengths()).toHaveLength(9);
      expect.soft(barSelect()?.value, 'the length entered comes back when the session offers it').toBe('15');
      expect.soft(barNote()).toBeUndefined();
    });

    it('says which length the run uses under extended hours too, when the server stops offering the one entered', () => {
      const component = fixture.componentInstance;
      const offered = signalBarMinutes.extended;
      signalBarMinutes.extended = [1, 2, 3, 4, 5, 6, 10, 12];
      try {
        component.range.update((r) => ({ ...r, multiplier: 15 }));
        fixture.detectChanges();

        expect.soft(component.shownRange().multiplier).toBe(12);
        expect.soft(barNote()).toBe('15-minute bars are not offered for this session, so this run uses 12-minute bars.');
        expect.soft(barNote(), 'the regular-hours reason is for regular hours only').not.toContain('Regular hours offer');
      } finally {
        signalBarMinutes.extended = offered;
      }
    });

    it('keeps a length the chosen session offers, and says only why the list is short', async () => {
      const component = fixture.componentInstance;
      component.range.update((r) => ({ ...r, multiplier: 3 }));
      fixture.detectChanges();

      await press('RTH');

      expect(component.shownRange().multiplier).toBe(3);
      expect(barNote()).not.toContain('are not offered for this session');
      expect(barNote()).toContain('Regular hours offer bars of 1, 2, 3, 4, 5, 6 minutes only');
      await press('Run study');
      expect(startJob.mock.calls[0][1]).toMatchObject({ session: 'rth', multiplier: 3 });
    });

    it.each([15, 12, 10])('never sends a length regular hours refuse, however the range was entered: %d-minute bars', async (multiplier) => {
      const component = fixture.componentInstance;
      component.range.update((r) => ({ ...r, session: 'rth', multiplier }));
      fixture.detectChanges();

      await component.runSignalEngine();

      expect(startJob.mock.calls[0][1]).toMatchObject({ session: 'rth', multiplier: 6 });
    });

    it('names the session in the sentence that says what is about to run', async () => {
      const summary = () => root().querySelector('.study-run__summary')?.textContent?.replace(/\s+/g, ' ').trim();
      expect.soft(summary()).toContain('extended hours');

      await press('RTH');

      expect.soft(summary()).toContain('regular hours');
      expect.soft(summary()).not.toContain('extended hours');
    });

    it('reads a range that names no session as extended hours, in the sentence and in the request', async () => {
      const component = fixture.componentInstance;
      component.range.set({ symbol: 'AAPL', from: '2024-01-02', to: '2024-03-28', resolution: 'minute', multiplier: 5 });
      fixture.detectChanges();

      expect.soft(root().querySelector('.study-run__summary')?.textContent).toContain('extended hours');
      expect.soft(screen.getByRole('button', { name: 'Ext' }).getAttribute('aria-pressed'), 'the picker shows what the run sends').toBe('true');
      await press('Run study');

      expect(startJob.mock.calls[0][1]).toMatchObject({ session: 'extended', multiplier: 5 });
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

  describe('sessions the lake does not hold yet', () => {
    const lacking = (missing: number) => ({ kind: 'ok' as const, value: heldReport({ sessions: SESSIONS, missing }) });

    /** Enters another window, which is what asks the lake for its count, and lets the page settle. */
    async function enterWindow(read: DataLakeRead<InstrumentDataReport>, to = '2024-06-27'): Promise<void> {
      instrumentData.mockResolvedValue(read);
      fixture.componentInstance.range.update((r) => ({ ...r, to }));
      fixture.detectChanges();
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));
      await fixture.whenStable();
      fixture.detectChanges();
    }

    const warning = () => screen.queryByText(/in the lake yet/);

    it('reads the count for the window the page holds, in the split-adjusted tree, and says nothing when the lake holds it all', async () => {
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(1));

      expect(instrumentData).toHaveBeenCalledWith({ symbol: 'AAPL', from: '2024-01-02', to: '2024-06-28', tree: 'polygon_split_adjusted' });
      expect(warning()).toBeNull();
    });

    it('warns how many of the run’s sessions the lake lacks and why the first run is slow, with an action to capture them', async () => {
      await enterWindow(lacking(12));

      expect(warning()?.textContent).toBe(`12 of ${SESSIONS} sessions aren’t in the lake yet.`);
      expect(screen.getByText('The first run fetches them one session at a time, so it will be slow.')).toBeTruthy();
      expect(screen.getByText('Capture them first.')).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Capture 12 sessions' })).toBeTruthy();
    });

    it('starts no capture by itself, and does not hold up Run study', async () => {
      await enterWindow(lacking(12));

      expect(captureRun).not.toHaveBeenCalled();
      expect((screen.getByRole('button', { name: 'Run study' }) as HTMLButtonElement).disabled).toBe(false);
      await userEvent.setup().click(screen.getByRole('button', { name: 'Run study' }));
      expect(startJob).toHaveBeenCalledOnce();
    });

    it('captures exactly the span the lake named through the shared runner, shows its progress, and counts again when it ends', async () => {
      const counted = lacking(12);
      await enterWindow(counted);
      let ends!: (end: TradeBackfillEnd) => void;
      let asked: TradeBackfillWindow | null = null;
      const job = { jobId: 'job-9', state: signal({ kind: 'running', started: true, progress: { current: 6, total: 24, unit: 'days', message: null } }), detach: vi.fn(), cancel: vi.fn() };
      captureRun.mockImplementation(async (select, accepted) => {
        asked = select(undefined as never);
        await accepted(job as unknown as BackfillJobRun);
        return new Promise<TradeBackfillEnd>((resolve) => (ends = resolve));
      });
      instrumentData.mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: SESSIONS }) });

      await userEvent.setup().click(screen.getByRole('button', { name: 'Capture 12 sessions' }));
      await fixture.whenStable();
      fixture.detectChanges();

      expect.soft(asked).toEqual({
        symbol: 'AAPL', price_adjustment_mode: 'polygon_split_adjusted',
        start_trading_date_ms: counted.value.fill_window?.start_trading_date_ms, end_trading_date_ms: counted.value.fill_window?.end_trading_date_ms,
      });
      expect.soft(screen.getByText('Capturing 12 sessions of AAPL into the lake.')).toBeTruthy();
      expect.soft(screen.getByText('6 of 24 days of the span')).toBeTruthy();
      expect.soft(screen.getByRole('button', { name: 'Cancel the capture' })).toBeTruthy();

      ends({ kind: 'completed', failure: null });
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(3));
      await fixture.whenStable();
      fixture.detectChanges();

      expect.soft(warning(), 'the lake holds it all now').toBeNull();
      expect.soft(screen.queryByText(/Capturing/)).toBeNull();
    });

    it('says a capture that stopped stopped, with the reason, and that Run study still fetches the rest', async () => {
      await enterWindow(lacking(12));
      captureRun.mockResolvedValue({ kind: 'failed', code: 'backfill_failed', message: 'the provider refused the key' });

      await userEvent.setup().click(screen.getByRole('button', { name: 'Capture 12 sessions' }));
      await vi.waitFor(() => expect(screen.getByText('The capture stopped before it finished.')).toBeTruthy());

      expect(screen.getByText(/the provider refused the key/)).toBeTruthy();
      expect(screen.getByText(/Run study fetches whatever is still missing/)).toBeTruthy();
    });

    it('says a count it could not read could not be read, and not that nothing is missing', async () => {
      await enterWindow({ kind: 'unavailable', message: 'The data plane did not respond.' });

      expect(screen.getByText('Could not check which of this run’s sessions the lake holds.')).toBeTruthy();
      expect(screen.getByText(/The data plane did not respond\./)).toBeTruthy();
      expect(warning()).toBeNull();
      expect(screen.queryByText(/0 of|none missing/i)).toBeNull();
    });

    it('reads the count again only for another instrument or window, not for another bar, session or indicator', async () => {
      const component = fixture.componentInstance;
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(1));
      const user = userEvent.setup();

      component.range.update((r) => ({ ...r, multiplier: 5 }));
      await user.click(screen.getByRole('button', { name: 'RTH' }));
      component.selectedIndicator.set('mom');
      fixture.detectChanges();
      await fixture.whenStable();
      expect(instrumentData, 'nothing the lake count is keyed on changed').toHaveBeenCalledTimes(1);

      component.range.update((r) => ({ ...r, symbol: 'MSFT' }));
      fixture.detectChanges();
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));
      expect(instrumentData).toHaveBeenLastCalledWith(expect.objectContaining({ symbol: 'MSFT' }));
    });

    it('reads nothing while the instrument or a date is not entered', async () => {
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(1));

      fixture.componentInstance.range.update((r) => ({ ...r, from: '' }));
      fixture.detectChanges();
      await fixture.whenStable();

      expect(instrumentData).toHaveBeenCalledTimes(1);
      expect(warning()).toBeNull();
    });
  });
});

describe('SignalRunnerComponent job wiring', () => {
  let component: SignalRunnerComponent;
  let fixture: ComponentFixture<SignalRunnerComponent>;
  let jobsById: ReturnType<typeof signal<Map<string, JobState>>>;
  let instrumentData: ReturnType<typeof vi.fn<(query: unknown) => Promise<DataLakeRead<InstrumentDataReport>>>>;
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

    // Left alone, the lake holds every session the default window needs, so no warning is drawn.
    instrumentData = vi.fn().mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: SESSIONS }) });

    await TestBed.configureTestingModule({
      imports: [SignalRunnerComponent, HttpClientTestingModule],
      providers: [
        { provide: JobsService, useValue: jobsServiceMock },
        { provide: DataLakeService, useValue: { instrumentData } },
        { provide: TradeBackfillRunner, useValue: { run: vi.fn() } },
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
    // tests/jobs/test_signal_study_job.py parses exactly this body. The session is named, not defaulted: the page starts
    // on extended hours, which the study has always computed on, and the request carries no second session field.
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
    calculation_version: 'signal-study/2',
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

  it('marks a shown result as from other inputs, naming the session it answers, once the session is switched', async () => {
    jobsServiceMock.fetchResult.mockResolvedValueOnce(SIGNAL_RESULT);
    await component.runSignalEngine();
    await settleJob('completed');
    expect.soft(screen.queryByText('Previous result'), 'it answers the inputs entered').toBeNull();
    const press = async (name: string) => {
      await userEvent.setup().click(screen.getByRole('button', { name }));
      await fixture.whenStable();
      fixture.detectChanges();
    };

    await press('RTH');

    expect.soft(screen.getByText('Previous result')).toBeTruthy();
    const answered = screen.getByText('Previous result').closest('.previous-result__banner') as HTMLElement;
    expect.soft(within(answered).getByText('Session').nextElementSibling?.textContent).toBe('extended hours');
    expect.soft(within(answered).getByText('Bars').nextElementSibling?.textContent, 'the bars it answers, not the ones now shown').toBe('15 minute');

    await press('Ext');

    expect.soft(screen.queryByText('Previous result'), 'switching back asks the question it answers again').toBeNull();
  });

  it('counts the sessions the lake lacks again once its own run ends, because the run filled the lake', async () => {
    instrumentData.mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: SESSIONS, missing: 12 }) });
    component.range.update((r) => ({ ...r, to: '2024-06-27' }));
    fixture.detectChanges();
    await vi.waitFor(() => expect(screen.queryByText(/in the lake yet/)).not.toBeNull());
    instrumentData.mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: SESSIONS }) });
    jobsServiceMock.fetchResult.mockResolvedValueOnce(SIGNAL_RESULT);
    const reads = instrumentData.mock.calls.length;

    await component.runSignalEngine();
    await settleJob('completed');
    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(reads + 1));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.queryByText(/in the lake yet/)).toBeNull();
  });

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

  it('names the session the lake judged in the refusal: the one its source records', async () => {
    const reason = 'The lake cannot admit this window: 1 of 82 sessions was captured before the session closed: 2024-03-06.';
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: false, status: 'refused', ticker: 'AAPL', error: reason,
      refusal: { code: 'captured_before_final', reason, sessions: [], sessions_total: 0, coverage: null },
      data_source: dataSource(null, { session_policy: 'regular', snapshot_digest: null, effective_window: null }),
    });

    const el = await settleJob('failed', { errorCode: 'captured_before_final', errorMessage: reason });

    expect(el.textContent).toContain('No result: the lake could not supply this study its data for regular hours.');
  });

  it('names the session the run asked for when the refusal came before anything was read', async () => {
    const reason = '7-minute bars do not divide the hour.';
    component.range.update((r) => ({ ...r, session: 'rth' }));
    await component.runSignalEngine();
    jobsServiceMock.fetchResult.mockResolvedValueOnce({
      success: false, status: 'refused', ticker: 'AAPL', error: reason,
      refusal: { code: 'resolution_unsupported', reason, sessions: [], sessions_total: 0, coverage: null },
      data_source: null,
    });

    const el = await settleJob('failed', { errorCode: 'resolution_unsupported', errorMessage: reason });

    expect(el.textContent).toContain('its data for regular hours.');
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
