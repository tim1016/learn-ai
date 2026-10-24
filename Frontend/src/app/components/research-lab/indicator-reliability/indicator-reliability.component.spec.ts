import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';

import { environment } from '../../../../environments/environment';
import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { IndicatorReliabilityComponent } from './indicator-reliability.component';

const RUN_URL = `${environment.pythonServiceUrl}/api/research/indicator-reliability`;

const group = (over: Record<string, number> = {}) => ({
  expected_sessions: 11,
  reused: 8,
  added: 3,
  missing: 0,
  failed: 0,
  building: 0,
  corrupt: 0,
  unavailable_from_provider: 0,
  ...over,
});

const dataSource = (over: Record<string, unknown> = {}) => ({
  kind: 'lake',
  provider: 'polygon',
  price_adjustment_mode: 'polygon_split_adjusted',
  session_policy: 'regular',
  bar_minutes: 1,
  aggregation: 'minute_fold_et_wall_clock_no_fill',
  window_start_ms: 1709528400000,
  window_end_ms: 1710478800000,
  calculation_version: 'indicator-reliability/1',
  identity_digest: 'a'.repeat(64),
  snapshot_digest: 'b'.repeat(64),
  result_identity_digest: 'c'.repeat(64),
  warmup: {
    policy: 'allow_shortened', requested_bars: 140, requested_sessions: 1, admitted_sessions: 1,
    admitted_bars: 390, shortened: false, shortfall_bars: 0, cause: 'none',
  },
  coverage: {
    window: group(),
    warmup: group({ expected_sessions: 1, reused: 0, added: 1 }),
    artifacts: { fetched: 4, reused: 8, refreshed: 0 },
  },
  ...over,
});

/** A completed response with no horizons or charts: enough for the page to render its run strip and source line. */
const completed = (source: unknown) => ({
  success: true,
  ticker: 'AAPL',
  indicator_name: 'rsi',
  indicator_params: { length: 14 },
  display_name: 'RSI (14)',
  category: 'momentum',
  start_date: '2024-01-02',
  end_date: '2024-06-28',
  bar_count: 1200,
  train_start: null, train_end: null, test_start: null, test_end: null, train_bars: null, test_bars: null,
  train_ratio: 0.7,
  results: [],
  slope_results: null,
  daily_ic_values: [],
  daily_ic_dates: [],
  best_horizon: null,
  any_significant_after_bonferroni: false,
  any_significant_after_fdr: false,
  num_horizons_tested: 5,
  random_simulations: 1000,
  verdict: null,
  decay_curve: [],
  regime_results: null,
  next_steps: [],
  info_footnotes: [],
  warnings: [],
  data_source: source,
  error: null,
});

describe('IndicatorReliabilityComponent: where the bars came from (#3523)', () => {
  let fixture: ComponentFixture<IndicatorReliabilityComponent>;
  let http: HttpTestingController;

  const page = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const text = (selector: string): string => page().querySelector(selector)?.textContent?.replace(/\s+/g, ' ').trim() ?? '';

  async function run(): Promise<ReturnType<HttpTestingController['expectOne']>> {
    fixture.componentInstance.runAnalysis();
    return http.expectOne(RUN_URL);
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [IndicatorReliabilityComponent, HttpClientTestingModule],
      providers: [
        provideFakeTickerCatalog(fakeTickerCatalog([{ symbol: 'AAPL', name: 'Apple Inc.', exchange: 'XNAS' }])),
        provideFakeVendorCatalog(fakeVendorCatalog([])),
        ...provideFakeDateWorld(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(IndicatorReliabilityComponent);
    fixture.detectChanges();
  });

  it('shows the lake as the origin with the session counts the run reached', async () => {
    (await run()).flush(completed(dataSource()));
    fixture.detectChanges();

    expect(text('.data-source-origin')).toBe('Lake');
    expect(text('.data-source-text')).toBe(
      'Polygon · split-adjusted · regular hours · 1-min bars · ' +
        'window 11 sessions: 8 reused · 3 added · 0 missing · 0 failed · warmup 1 session: 0 reused · 1 added · 0 missing · 0 failed',
    );
    expect(page().textContent).not.toContain('Warmup is');
  });

  it('warns that the warmup was short, and why, when the lake could not supply the whole lead-in', async () => {
    const shortened = dataSource({
      warmup: {
        policy: 'allow_shortened', requested_bars: 1000, requested_sessions: 3, admitted_sessions: 1,
        admitted_bars: 390, shortened: true, shortfall_bars: 610, cause: 'provider_history_floor',
      },
    });
    (await run()).flush(completed(shortened));
    fixture.detectChanges();

    expect(page().textContent).toContain("Warmup is 610 bars short of the 1000 the policy asks for");
    expect(page().textContent).toContain("the provider's history does not reach back further");
  });

  it('shows a refusal in the study\'s own words with the counts admission reached, and no result', async () => {
    const refused = dataSource({
      snapshot_digest: null,
      result_identity_digest: null,
      warmup: null,
      coverage: { window: group({ reused: 0, added: 9, failed: 1, unavailable_from_provider: 1 }), warmup: group({ expected_sessions: 1, reused: 0, added: 1 }), artifacts: { fetched: 9, reused: 0, refreshed: 0 } },
    });
    const message = 'The lake cannot admit this window: 1 of 12 sessions cannot be supplied by the provider: 2024-03-08 (provider_no_data).';
    (await run()).flush(
      { detail: { code: 'data_unavailable_from_provider', message, sessions: [], sessions_total: 1, coverage: refused.coverage, data_source: refused } },
      { status: 409, statusText: 'Conflict' },
    );
    fixture.detectChanges();

    expect(text('p-message')).toBe(message);
    expect(text('.data-source-text')).toContain('window 11 sessions: 0 reused · 9 added · 0 missing · 1 failed');
    expect(fixture.componentInstance.result()).toBeNull();
  });

  it('shows a plain-text error as it always did and no lake source line', async () => {
    (await run()).flush({ detail: 'Maximum horizon is 100 bars' }, { status: 400, statusText: 'Bad Request' });
    fixture.detectChanges();

    expect(text('p-message')).toBe('Maximum horizon is 100 bars');
    expect(page().querySelector('app-data-source')).toBeNull();
  });
});
