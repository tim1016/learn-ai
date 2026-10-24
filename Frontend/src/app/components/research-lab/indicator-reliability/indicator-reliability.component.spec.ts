import { vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';

import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { environment } from '../../../../environments/environment';
import { RECORD_ID, RELIABILITY_INPUTS, storedRecord } from '../../../research/stored-study/testing/stored-study-fixtures';
import { DataLakeService, type DataLakeRead, type InstrumentDataReport } from '../../../shared/data-lake';
import type { BackfillJobRun } from '../../../shared/data-lake/backfill-job-runner';
import { TradeBackfillRunner, type TradeBackfillEnd } from '../../../shared/data-lake/trade-backfill-runner';
import type { TradeBackfillWindow } from '../../../shared/data-lake/trade-backfill-spec';
import { heldReport } from '../../../shared/data-lake/testing/instrument-data-report';
import { IndicatorReliabilityComponent } from './indicator-reliability.component';

describe('IndicatorReliabilityComponent (Indicator reliability study)', () => {
  let fixture: ComponentFixture<IndicatorReliabilityComponent>;
  let component: IndicatorReliabilityComponent;
  let http: HttpTestingController;
  // The lake's count of the sessions the run needs (read once per span), and the shared capture.
  let instrumentData: ReturnType<typeof vi.fn<(query: unknown) => Promise<DataLakeRead<InstrumentDataReport>>>>;
  let captureRun: ReturnType<typeof vi.fn<(select: (defaults: never) => TradeBackfillWindow, accepted: (job: BackfillJobRun) => boolean | Promise<boolean>) => Promise<TradeBackfillEnd>>>;

  const root = () => fixture.nativeElement as HTMLElement;
  const runs = () => http.match((request) => request.method === 'POST' && request.url.endsWith('/api/research/indicator-reliability'));
  const paramReads = () => http.match((request) => request.url.includes('/api/research/indicator-params/'));
  const spanReads = () => http.match((request) => request.method === 'POST' && request.url.endsWith('/api/research/indicator-reliability/span'));
  const SCHEMA = [{ name: 'length', type: 'int', default: 14, min: 1, max: 200, description: 'Lookback.' }];
  /**
   * What the server names for the default window (2024-01-02 to 2024-06-28) and RSI(14): one lead-in session, Friday
   * 2023-12-29, ahead of it. ET midnights, `int64 ms UTC`.
   */
  const SPAN = { start_ms: Date.UTC(2023, 11, 29, 5), end_ms: Date.UTC(2024, 5, 28, 4), warmup_sessions: 1 };
  /** The indicator's parameters arrive; until they do, Run study is off. The span read they let go out is answered too. */
  const answerParams = () => {
    for (const request of paramReads()) request.flush(SCHEMA);
    fixture.detectChanges();
    for (const request of spanReads()) request.flush(SPAN);
  };
  const runButton = () => root().querySelector<HTMLButtonElement>('button.study-run');

  beforeEach(async () => {
    // Left alone, the lake holds every session the run needs, so only the specs about the count see a warning.
    instrumentData = vi.fn().mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: 125 }) });
    captureRun = vi.fn().mockResolvedValue({ kind: 'completed', failure: null });
    await TestBed.configureTestingModule({
      imports: [IndicatorReliabilityComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: DataLakeService, useValue: { instrumentData } },
        { provide: TradeBackfillRunner, useValue: { run: captureRun } },
        provideFakeTickerCatalog(
          fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' }]),
        ),
        provideFakeVendorCatalog(fakeVendorCatalog([])),
        ...provideFakeDateWorld(),
      ],
    }).compileComponents();

    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(IndicatorReliabilityComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('offers the session as its one control, regular hours chosen, and says the bars are fixed instead of offering other sizes', () => {
    const rth = screen.getByRole('button', { name: 'RTH' }) as HTMLButtonElement;
    const ext = screen.getByRole('button', { name: 'Ext' }) as HTMLButtonElement;

    expect.soft(rth.disabled).toBe(false);
    expect.soft(ext.disabled).toBe(false);
    expect.soft(rth.getAttribute('aria-pressed'), 'regular hours is what the study has always computed on').toBe('true');
    expect.soft(ext.getAttribute('aria-pressed')).toBe('false');
    expect.soft(Array.from(root().querySelectorAll('.res-btn')).map((el) => el.textContent?.trim()), 'one-minute bars only').toEqual(['1m']);
    expect.soft(root().querySelector('select[aria-label="Bar multiplier"]'), 'no other bar length is offered').toBeNull();
    expect.soft(root().querySelector('.autofetch'), 'the study fetches nothing on its own').toBeNull();
    const fixed = root().querySelector('[data-testid="study-constraints"]')?.textContent?.replace(/\s+/g, ' ') ?? '';
    expect.soft(fixed).toContain('1 minute, fixed');
    expect.soft(fixed, 'the session is a choice now').not.toContain('regular trading hours, fixed');
    // The default window is long enough for the picker's hour-bars hint.
    expect.soft(root().textContent, 'no hint to switch to bars the study does not read').not.toContain('Switch to hour');
  });

  it('starts nothing when an input changes', () => {
    component.range.update((r) => ({ ...r, symbol: 'MSFT', from: '2024-02-01' }));
    component.indicatorName.set('macd');
    component.selectedHorizons.set([1, 5]);
    component.includeSlope.set(true);
    fixture.detectChanges();

    expect(runs()).toHaveLength(0);
  });

  it('sends one request when Run study is pressed twice before the page re-renders', () => {
    answerParams();
    const run = runButton();
    if (run === null) throw new Error('no Run study button');

    run.click();
    run.click();

    expect(runs()).toHaveLength(1);
  });

  it('keeps Run study off until the indicator’s parameters have arrived', () => {
    expect(runButton()?.disabled, 'the parameters are still being read').toBe(true);
    expect(root().querySelector('.study-run__summary')?.textContent).toContain('Waiting for the indicator’s parameters');

    answerParams();

    expect(runButton()?.disabled).toBe(false);
  });

  it('shows a failed parameter read in step 2, keeps Run study off, and runs again once a retry succeeds', () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    for (const request of paramReads()) request.flush({ detail: 'schema unavailable' }, { status: 500, statusText: 'Server Error' });
    fixture.detectChanges();

    const problem = root().querySelector('[data-testid="params-error"]');
    expect.soft(problem?.textContent).toContain('The parameters of RSI could not be loaded: schema unavailable.');
    expect.soft(runButton()?.disabled).toBe(true);
    expect.soft(root().querySelector('.study-run__summary')?.textContent).toContain('use Try again in step 2');

    problem?.querySelector<HTMLButtonElement>('button.config-retry')?.click();
    answerParams();

    expect.soft(root().querySelector('[data-testid="params-error"]')).toBeNull();
    expect.soft(runButton()?.disabled).toBe(false);
    expect(errors, 'a failure is shown to the reader, not written to the console').not.toHaveBeenCalled();
  });

  it('shows a failed indicator list in step 2 instead of leaving the picker silently empty', () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    http
      .expectOne(`${environment.pythonServiceUrl}/api/research/indicators`)
      .flush({}, { status: 503, statusText: 'Unavailable' });
    fixture.detectChanges();

    expect(root().querySelector('[data-testid="indicators-error"]')?.textContent).toContain(
      'The indicator list could not be loaded: the data plane answered 503.',
    );
    expect(errors).not.toHaveBeenCalled();
  });

  it('fills the inputs from a saved run, keeps its parameters over the indicator’s defaults, starts nothing, then names the run when Run study is pressed', async () => {
    fixture.componentRef.setInput('rerun', RECORD_ID);
    fixture.detectChanges();
    http
      .expectOne(`${environment.pythonServiceUrl}/api/research/study-records/indicator-reliability/${RECORD_ID}`)
      .flush(storedRecord('indicator-reliability'));
    await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
    await fixture.whenStable();
    // The indicator's own parameters arrive after the fill; the saved ones stay.
    for (const request of http.match((r) => r.url.includes('/api/research/indicator-params/'))) {
      request.flush([{ name: 'length', type: 'int', default: 10, min: 1, max: 200, description: 'Lookback.' }]);
    }
    fixture.detectChanges();
    for (const request of spanReads()) request.flush(SPAN);
    await fixture.whenStable();

    expect(component.range()).toMatchObject({ symbol: 'SPY', from: '2024-01-02', to: '2024-03-28' });
    expect(component.indicatorName()).toBe('rsi');
    expect(component.paramValues()).toEqual({ length: 14 });
    expect(component.selectedHorizons()).toEqual([5, 1]);
    expect(runs()).toHaveLength(0);

    component.runAnalysis();

    const [run] = runs();
    expect(run.request.body).toMatchObject({ indicator_name: 'rsi', horizons: [5, 1], rerun_of: RECORD_ID });
  });

  describe('the session', () => {
    /** Presses a button of the page and lets the page settle. */
    async function press(name: string): Promise<void> {
      await userEvent.setup().click(screen.getByRole('button', { name }));
      await fixture.whenStable();
      fixture.detectChanges();
    }
    const summary = () => root().querySelector('.study-run__summary')?.textContent?.replace(/\s+/g, ' ').trim();

    it('sends the session that was pressed, and regular hours when nothing was', async () => {
      answerParams();
      const sent: unknown[] = [];
      const runOnce = async () => {
        runButton()?.click();
        for (const request of runs()) {
          sent.push((request.request.body as { session: string }).session);
          request.flush({ detail: 'stop here' }, { status: 400, statusText: 'Bad Request' });
        }
        await fixture.whenStable();
        fixture.detectChanges();
      };

      await runOnce();
      await press('Ext');
      await runOnce();
      await press('RTH');
      await runOnce();

      expect(sent).toEqual(['rth', 'extended', 'rth']);
    });

    it('names the session in the sentence that says what is about to run', async () => {
      answerParams();
      expect.soft(summary()).toContain('regular hours');

      await press('Ext');

      expect.soft(summary()).toContain('extended hours');
      expect.soft(summary()).not.toContain('regular hours');
    });

    it('reads a range that names no session as regular hours, in the sentence and in the request', () => {
      component.range.set({ symbol: 'AAPL', from: '2024-01-02', to: '2024-06-28', resolution: 'minute' });
      answerParams();

      expect.soft(summary()).toContain('regular hours');
      expect.soft(screen.getByRole('button', { name: 'RTH' }).getAttribute('aria-pressed'), 'the picker shows what the run sends').toBe('true');
      runButton()?.click();
      expect(runs()[0].request.body).toMatchObject({ session: 'rth' });
    });

    describe('a restored saved run', () => {
      const recordUrl = `${environment.pythonServiceUrl}/api/research/study-records/indicator-reliability/${RECORD_ID}`;

      async function openSaved(inputs: object): Promise<void> {
        fixture.componentRef.setInput('rerun', RECORD_ID);
        fixture.detectChanges();
        http.expectOne(recordUrl).flush(storedRecord('indicator-reliability', { inputs: inputs as never }));
        await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
        await fixture.whenStable();
        fixture.detectChanges();
        answerParams();
        await fixture.whenStable();
      }

      it.each([
        ['regular hours', 'rth', 'RTH'],
        ['extended hours', 'extended', 'Ext'],
      ])('fills the session the saved run computed on: %s', async (_name, session, button) => {
        await openSaved({ ...RELIABILITY_INPUTS, session });

        expect(component.range().session).toBe(session);
        expect(screen.getByRole('button', { name: button }).getAttribute('aria-pressed')).toBe('true');
      });

      it('fills regular hours for a saved run that names no session, which is what the study computed on before it did', async () => {
        const unnamed = Object.fromEntries(Object.entries(RELIABILITY_INPUTS).filter(([key]) => key !== 'session'));
        await press('Ext');

        await openSaved(unnamed);

        expect(component.range().session).toBe('rth');
      });

      it('fills nothing when the saved run’s session is not one this page offers', async () => {
        const before = component.range();

        await openSaved({ ...RELIABILITY_INPUTS, session: 'overnight' });

        expect(component.range()).toBe(before);
        expect(root().querySelector('[data-testid="study-prefill"]')?.textContent).toContain('could not be read');
      });
    });
  });

  describe('sessions the lake does not hold yet', () => {
    const warning = () => screen.queryByText(/in the lake yet/);
    it('reads nothing while the indicator’s parameters have not arrived', async () => {
      await fixture.whenStable();

      expect(spanReads()).toHaveLength(0);
      expect(instrumentData).not.toHaveBeenCalled();
    });

    it('asks the server which sessions the run needs, then counts the lake over that span: the lead-in the server sized, not the typed start', async () => {
      for (const request of paramReads()) request.flush(SCHEMA);
      fixture.detectChanges();

      const [span, ...others] = spanReads();
      expect(others).toHaveLength(0);
      expect(span.request.body).toEqual({
        symbol: 'AAPL', from_date: '2024-01-02', to_date: '2024-06-28', timespan: 'minute', multiplier: 1,
        indicator_name: 'rsi', indicator_params: { length: 14 },
      });
      expect(instrumentData, 'the lake is asked about nothing until the span is named').not.toHaveBeenCalled();

      span.flush(SPAN);
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(1));

      expect(instrumentData).toHaveBeenCalledWith({ symbol: 'AAPL', from: '2023-12-29', to: '2024-06-28', tree: 'polygon_split_adjusted' });
      await fixture.whenStable();
      fixture.detectChanges();
      expect(warning(), 'the lake holds it all').toBeNull();
    });

    it('asks about the window and bar size the run itself sends, from the same wire, and sends no session', () => {
      for (const request of paramReads()) request.flush(SCHEMA);
      fixture.detectChanges();
      const [span] = spanReads();
      runButton()?.click();
      const [run] = runs();
      const spanBody = span.request.body as Record<string, unknown>;
      const runBody = run.request.body as Record<string, unknown>;

      for (const key of ['symbol', 'from_date', 'to_date', 'timespan', 'multiplier', 'indicator_name', 'indicator_params']) {
        expect(spanBody[key], key).toEqual(runBody[key]);
      }
      expect(Object.keys(spanBody)).not.toContain('session');
      span.flush(SPAN);
    });

    it('warns how many sessions the lake lacks over that span, with the action that captures them, and Run study stays on', async () => {
      instrumentData.mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: 125, missing: 30 }) });
      answerParams();
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(1));
      await fixture.whenStable();
      fixture.detectChanges();

      expect(warning()?.textContent).toBe('30 of 125 sessions aren’t in the lake yet.');
      expect(screen.getByText('The first run fetches them one session at a time, so it will be slow.')).toBeTruthy();
      expect((screen.getByRole('button', { name: 'Run study' }) as HTMLButtonElement).disabled).toBe(false);
      expect(captureRun, 'nothing captures by itself').not.toHaveBeenCalled();

      let asked: TradeBackfillWindow | null = null;
      captureRun.mockImplementation(async (select) => {
        asked = select(undefined as never);
        return { kind: 'completed', failure: null } as TradeBackfillEnd;
      });
      const named = (instrumentData.mock.results[0].value as Promise<{ value: InstrumentDataReport }>);
      const fill = (await named).value.fill_window;
      await userEvent.setup().click(screen.getByRole('button', { name: 'Capture 30 sessions' }));

      expect(asked).toEqual({
        symbol: 'AAPL', price_adjustment_mode: 'polygon_split_adjusted',
        start_trading_date_ms: fill?.start_trading_date_ms, end_trading_date_ms: fill?.end_trading_date_ms,
      });
    });

    it('says nothing of the lake, and offers no capture, while the server is still naming the span', () => {
      for (const request of paramReads()) request.flush(SCHEMA);
      fixture.detectChanges();

      const unanswered = spanReads();
      expect(unanswered, 'the span read is out and unanswered').toHaveLength(1);
      expect(screen.queryByText(/Could not check which/), 'a span being read is not a span that failed').toBeNull();
      expect(warning()).toBeNull();
      expect(screen.queryByRole('button', { name: /^Capture/ })).toBeNull();
      expect(instrumentData).not.toHaveBeenCalled();
      unanswered[0].flush(SPAN);
    });

    it('says a span the server could not name could not be checked, and not that nothing is missing', async () => {
      for (const request of paramReads()) request.flush(SCHEMA);
      fixture.detectChanges();
      for (const request of spanReads()) request.flush({ detail: 'the calendar is unavailable' }, { status: 503, statusText: 'Unavailable' });
      await fixture.whenStable();
      fixture.detectChanges();

      expect(screen.getByText('Could not check which of this run’s sessions the lake holds.')).toBeTruthy();
      expect(screen.getByText(/the calendar is unavailable/)).toBeTruthy();
      expect(instrumentData).not.toHaveBeenCalled();
      expect(warning()).toBeNull();
      expect((screen.getByRole('button', { name: 'Run study' }) as HTMLButtonElement).disabled).toBe(false);
    });

    it('reads the span again for another instrument, window or indicator parameter, not for another session', async () => {
      answerParams();
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(1));
      await fixture.whenStable();

      await userEvent.setup().click(screen.getByRole('button', { name: 'Ext' }));
      fixture.detectChanges();
      await fixture.whenStable();
      expect(spanReads(), 'the lead-in does not depend on the session').toHaveLength(0);
      expect(instrumentData).toHaveBeenCalledTimes(1);

      component.paramValues.set({ length: 200 });
      fixture.detectChanges();
      const [another] = spanReads();
      expect(another.request.body).toMatchObject({ indicator_params: { length: 200 } });
      another.flush({ start_ms: Date.UTC(2023, 11, 27, 5), end_ms: SPAN.end_ms, warmup_sessions: 3 });
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));

      expect(instrumentData).toHaveBeenLastCalledWith({ symbol: 'AAPL', from: '2023-12-27', to: '2024-06-28', tree: 'polygon_split_adjusted' });
    });

    it('draws no count for a span that answers inputs since changed', async () => {
      instrumentData.mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: 125, missing: 30 }) });
      for (const request of paramReads()) request.flush(SCHEMA);
      fixture.detectChanges();
      const [stale] = spanReads();

      component.range.update((r) => ({ ...r, symbol: 'MSFT' }));
      fixture.detectChanges();
      stale.flush(SPAN);
      await new Promise((resolve) => setTimeout(resolve)); // the stale answer resolves in a later task than the flush
      fixture.detectChanges();

      expect(instrumentData, 'the AAPL span is not MSFT’s').not.toHaveBeenCalled();
      expect(warning()).toBeNull();
      const [current] = spanReads();
      expect(current.request.body).toMatchObject({ symbol: 'MSFT' });
    });

    it('counts again once its own run ends, because the run filled the lake', async () => {
      instrumentData.mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: 125, missing: 30 }) });
      answerParams();
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(1));
      await fixture.whenStable();
      fixture.detectChanges();
      expect(warning()).not.toBeNull();
      instrumentData.mockResolvedValue({ kind: 'ok', value: heldReport({ sessions: 125 }) });

      runButton()?.click();
      runs()[0].flush({ detail: 'stop here' }, { status: 400, statusText: 'Bad Request' });
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));
      await fixture.whenStable();
      fixture.detectChanges();

      expect(warning()).toBeNull();
    });
  });

  describe('where the bars came from (#3523)', () => {
    const MONDAY = Date.UTC(2024, 2, 4, 5, 0);
    const FRIDAY = Date.UTC(2024, 2, 15, 4, 0);
    const group = (over: Record<string, number> = {}) => ({
      expected_sessions: 11, reused: 8, added: 3, missing: 0, failed: 0,
      building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 0, ...over,
    });
    const WARMUP = {
      policy: 'allow_shortened', requested_bars: 140, requested_sessions: 1, admitted_sessions: 1,
      admitted_bars: 390, shortened: false, shortfall_bars: 0, cause: 'none', notice: null as string | null,
    };
    const dataSource = (over: Record<string, unknown> = {}) => ({
      kind: 'lake',
      provider: 'polygon',
      symbol: 'AAPL',
      price_adjustment_mode: 'polygon_split_adjusted',
      session_policy: 'regular',
      window_start_ms: MONDAY,
      window_end_ms: FRIDAY,
      effective_window: { start_ms: MONDAY, end_ms: FRIDAY },
      warmup_sessions: 1,
      bar_minutes: 1,
      aggregation: 'minute_fold_et_wall_clock_no_fill',
      calculation_version: 'indicator-reliability/1',
      identity_digest: 'a'.repeat(64),
      snapshot_digest: 'b'.repeat(64),
      result_identity_digest: 'c'.repeat(64),
      data_root_id: 'root',
      corporate_action_versions: {},
      minute_bars_read: 4680,
      bars_used: 4290,
      cache: { hit: false, cached_at_ms: null },
      warmup: WARMUP,
      coverage: {
        window: group(),
        warmup: group({ expected_sessions: 1, reused: 0, added: 1 }),
        artifacts: { fetched: 4, reused: 8, refreshed: 0 },
      },
      ...over,
    });
    /** A completed response with no horizons or charts: enough for the page to render its report and source line. */
    const completed = (source: unknown) => ({
      success: true, ticker: 'AAPL', indicator_name: 'rsi', indicator_params: { length: 14 },
      display_name: 'RSI (14)', category: 'momentum', start_date: '2024-01-02', end_date: '2024-06-28',
      bar_count: 1200,
      train_start: null, train_end: null, test_start: null, test_end: null, train_bars: null, test_bars: null,
      train_ratio: 0.7, results: [], slope_results: null, daily_ic_values: [], daily_ic_dates: [],
      best_horizon: null, any_significant_after_bonferroni: false, any_significant_after_fdr: false,
      num_horizons_tested: 5, random_simulations: 1000, verdict: null, decay_curve: [], regime_results: null,
      next_steps: [], info_footnotes: [], warnings: [], data_source: source, error: null,
    });
    /** The body of a refused run: the shared refusal a job parks, answered here as the 409's body. */
    const refused = (refusal: Record<string, unknown>, source: unknown) => ({
      success: false, status: 'refused', ticker: 'AAPL', error: refusal['reason'], refusal, data_source: source,
    });
    const lines = () => root().querySelectorAll('app-workspace-data-source');

    /** Press Run study and answer the request it makes. */
    function run(answer: (request: TestRequest) => void): void {
      answerParams();
      runButton()?.click();
      const [request] = runs();
      if (request === undefined) throw new Error('Run study sent no request');
      answer(request);
      fixture.detectChanges();
    }

    it('names the lake before anything has run, not a provider fetch', () => {
      expect(lines()).toHaveLength(1);
      expect(lines()[0].textContent).toContain('Data lake');
    });

    it('sends the regular session the study computes on, and shows the lake source with the session counts the run reached', () => {
      run((request) => {
        expect(request.request.body).toMatchObject({ symbol: 'AAPL', timespan: 'minute', multiplier: 1, session: 'rth' });
        request.flush(completed(dataSource()));
      });

      expect(lines(), 'one data-source line, the layout\'s').toHaveLength(1);
      const line = lines()[0].textContent ?? '';
      expect(line).toContain('Data lake, Polygon');
      expect(line).toContain('4 added · 8 reused · 0 missing');
      expect(line).toContain('1 session before the window');
      expect(line, 'a window of dates carries no time of day').not.toMatch(/\d{1,2}:\d{2}/);
      expect(root().textContent).not.toContain('Warmup is');
    });

    it('sends extended hours when they are picked, and shows the extended source the run came back with', async () => {
      answerParams();
      await userEvent.setup().click(screen.getByRole('button', { name: 'Ext' }));
      fixture.detectChanges();
      runButton()?.click();
      const [request] = runs();
      expect(request.request.body).toMatchObject({ symbol: 'AAPL', timespan: 'minute', multiplier: 1, session: 'extended' });

      request.flush(completed(dataSource({ session_policy: 'extended' })));
      fixture.detectChanges();

      expect(lines()[0].textContent).toMatch(/Session policy\s*Extended/);
    });

    describe('a shown result', () => {
      const previous = () => screen.queryByText('Previous result');

      it('is marked as from other inputs, naming the session it answers, once the session is switched', async () => {
        run((request) => request.flush(completed(dataSource())));
        expect.soft(previous(), 'it answers the inputs entered').toBeNull();

        await userEvent.setup().click(screen.getByRole('button', { name: 'Ext' }));
        fixture.detectChanges();

        expect.soft(previous()).toBeTruthy();
        const answered = screen.getByText('Previous result').closest('.previous-result__banner') as HTMLElement;
        expect.soft(within(answered).getByText('Session').nextElementSibling?.textContent).toBe('regular hours');

        await userEvent.setup().click(screen.getByRole('button', { name: 'RTH' }));
        fixture.detectChanges();

        expect.soft(previous(), 'switching back asks the question it answers again').toBeNull();
      });
    });

    it('names the session a refusal judged: the one its source records, or the one asked for when nothing was read', async () => {
      const early = 'The lake cannot admit this window: 1 of 12 sessions was captured before the session closed: 2024-03-06.';
      answerParams();
      await userEvent.setup().click(screen.getByRole('button', { name: 'Ext' }));
      fixture.detectChanges();
      runButton()?.click();
      runs()[0].flush(
        refused(
          { code: 'captured_before_final', reason: early, sessions: [], sessions_total: 0, coverage: null },
          dataSource({ session_policy: 'extended', snapshot_digest: null, effective_window: null, warmup: null, coverage: null }),
        ),
        { status: 409, statusText: 'Conflict' },
      );
      fixture.detectChanges();

      expect.soft(root().textContent).toContain('No result: the lake could not supply this study its data for extended hours.');

      const before = 'Daily bars are not read from the lake.';
      runButton()?.click();
      runs()[0].flush(
        refused({ code: 'resolution_unsupported', reason: before, sessions: [], sessions_total: 0, coverage: null }, null),
        { status: 409, statusText: 'Conflict' },
      );
      fixture.detectChanges();

      expect.soft(root().textContent, 'nothing was read, so the session the run asked for is named').toContain('its data for extended hours.');
    });

    it('warns, in the server\'s words, that the warmup was short when the lake could not supply the whole lead-in', () => {
      const notice =
        "Warmup is 610 bars short of the 1000 the sizing asks for (the provider's history does not reach back further); " +
        "the indicator's first values may be less settled.";
      const shortened = dataSource({
        warmup: {
          ...WARMUP, requested_bars: 1000, requested_sessions: 3, shortened: true, shortfall_bars: 610,
          cause: 'provider_history_floor', notice,
        },
      });

      run((request) => request.flush(completed(shortened)));

      expect(root().querySelector('p-message')?.textContent).toContain(notice);
      expect(lines()[0].textContent).toContain('Data lake, Polygon');
    });

    it('shows a refused run as no result, with the reason, the counts admission reached and the session it could not make ready', () => {
      const reason = 'The lake cannot admit this window: 1 of 12 sessions cannot be supplied by the provider: 2024-03-08 (provider_no_data).';
      const counts = group({ added: 9, reused: 0, failed: 1, unavailable_from_provider: 1 });
      const coverage = {
        window: counts,
        warmup: group({ expected_sessions: 1, reused: 0, added: 1 }),
        artifacts: { fetched: 9, reused: 0, refreshed: 0 },
      };
      const refusal = {
        code: 'data_unavailable_from_provider',
        reason,
        sessions: [{ group: 'window', trading_date_ms: MONDAY, outcome: 'unavailable_from_provider', reason: 'provider_no_data', failure_reason: 'provider_no_data' }],
        sessions_total: 1,
        coverage,
      };

      run((request) =>
        request.flush(refused(refusal, dataSource({ snapshot_digest: null, effective_window: null, warmup: null, coverage })), {
          status: 409,
          statusText: 'Conflict',
        }),
      );

      const text = root().textContent ?? '';
      expect(text).toContain('No result');
      expect(text.split(reason).length - 1, 'the reason is said once, not as an error banner as well').toBe(1);
      expect(text).toContain('11 sessions · 0 reused · 9 added · 0 missing · 1 failed (1 unavailable from the provider)');
      expect(text).toContain('provider_no_data');
      expect(root().querySelector('p-message')).toBeNull();
      expect(text).not.toContain('RSI (14)');
      expect(lines(), 'the refused source is the layout\'s one line').toHaveLength(1);
      expect(lines()[0].textContent).toContain('Data lake, Polygon');
    });

    it('shows a refusal that came before anything was read with its reason and no counts', () => {
      const reason = 'Daily bars are not read from the lake: its daily rollup folds extended-hours trades and is not the vendor\'s daily bar.';
      const refusal = { code: 'resolution_unsupported', reason, sessions: [], sessions_total: 0, coverage: null };

      run((request) => request.flush(refused(refusal, null), { status: 409, statusText: 'Conflict' }));

      const text = root().textContent ?? '';
      expect(text).toContain(reason);
      expect(text, 'nothing was admitted, so no counts are claimed').not.toContain('sessions ·');
    });

    it('shows a plain-text error as it always did and no lake refusal', () => {
      run((request) => request.flush({ detail: 'Maximum horizon is 100 bars' }, { status: 400, statusText: 'Bad Request' }));

      expect(root().querySelector('p-message')?.textContent).toContain('Maximum horizon is 100 bars');
      expect(root().querySelector('app-lake-study-source')).toBeNull();
      expect(root().textContent).not.toContain('No result');
    });
  });
});
