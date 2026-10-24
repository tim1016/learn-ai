import { vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';

import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { environment } from '../../../../environments/environment';
import { RECORD_ID, storedRecord } from '../../../research/stored-study/testing/stored-study-fixtures';
import { IndicatorReliabilityComponent } from './indicator-reliability.component';

describe('IndicatorReliabilityComponent (Indicator reliability study)', () => {
  let fixture: ComponentFixture<IndicatorReliabilityComponent>;
  let component: IndicatorReliabilityComponent;
  let http: HttpTestingController;

  const root = () => fixture.nativeElement as HTMLElement;
  const runs = () => http.match((request) => request.method === 'POST' && request.url.endsWith('/api/research/indicator-reliability'));
  const paramReads = () => http.match((request) => request.url.includes('/api/research/indicator-params/'));
  const SCHEMA = [{ name: 'length', type: 'int', default: 14, min: 1, max: 200, description: 'Lookback.' }];
  /** The indicator's parameters arrive; until they do, Run study is off. */
  const answerParams = () => {
    for (const request of paramReads()) request.flush(SCHEMA);
    fixture.detectChanges();
  };
  const runButton = () => root().querySelector<HTMLButtonElement>('button.study-run');

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [IndicatorReliabilityComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
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

  it('says its session and bars are fixed instead of offering controls for them', () => {
    expect.soft(root().querySelector('app-sampling-card'), 'no resolution or session control').toBeNull();
    const fixed = root().querySelector('[data-testid="study-constraints"]')?.textContent?.replace(/\s+/g, ' ') ?? '';
    expect.soft(fixed).toContain('regular trading hours, fixed');
    expect.soft(fixed).toContain('1 minute, fixed');
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
