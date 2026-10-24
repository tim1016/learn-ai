import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { IndicatorReliabilityComponent } from './indicator-reliability.component';

const RUN_URL = `${environment.pythonServiceUrl}/api/research/indicator-reliability`;
const MONDAY = Date.UTC(2024, 2, 4, 5, 0);
const FRIDAY = Date.UTC(2024, 2, 15, 4, 0);

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

/** The body of a refused run: the shared refusal the server parks for a job, answered here as the 409's body. */
const refused = (refusal: Record<string, unknown>, source: unknown) => ({
  success: false,
  status: 'refused',
  ticker: 'AAPL',
  error: refusal['reason'],
  refusal,
  data_source: source,
});

async function renderPage() {
  const view = await render(IndicatorReliabilityComponent, {
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      provideFakeTickerCatalog(fakeTickerCatalog([{ symbol: 'AAPL', name: 'Apple Inc.', exchange: 'XNAS' }])),
      provideFakeVendorCatalog(fakeVendorCatalog([])),
      ...provideFakeDateWorld(),
    ],
  });
  const http = TestBed.inject(HttpTestingController);

  /** Click Run and answer the request it makes; the page's other requests (the indicator catalog) stay unanswered. */
  async function run(answer: (request: ReturnType<HttpTestingController['expectOne']>) => void): Promise<void> {
    await userEvent.click(screen.getByRole('button', { name: /run analysis/i }));
    answer(http.expectOne(RUN_URL));
    view.fixture.detectChanges();
  }
  return { run };
}

describe('IndicatorReliabilityComponent: where the bars came from (#3523)', () => {
  it('sends the regular session the study computes on, and shows the lake source with the session counts the run reached', async () => {
    const { run } = await renderPage();

    await run((request) => {
      expect(request.request.body).toMatchObject({ symbol: 'AAPL', timespan: 'minute', multiplier: 1, session: 'rth' });
      request.flush(completed(dataSource()));
    });

    expect(screen.getByText('Data source')).toBeTruthy();
    expect(screen.getByText('4 added · 8 reused · 0 missing')).toBeTruthy();
    expect(document.body.textContent).toContain('Data lake, Polygon');
    expect(document.body.textContent).toContain('1 session before the window');
    expect(screen.queryByText(/Warmup is/)).toBeNull();
  });

  it('warns, in the server\'s words, that the warmup was short when the lake could not supply the whole lead-in', async () => {
    const notice =
      "Warmup is 610 bars short of the 1000 the sizing asks for (the provider's history does not reach back further); " +
      "the indicator's first values may be less settled.";
    const shortened = dataSource({
      warmup: {
        ...WARMUP, requested_bars: 1000, requested_sessions: 3, shortened: true, shortfall_bars: 610,
        cause: 'provider_history_floor', notice,
      },
    });
    const { run } = await renderPage();

    await run((request) => request.flush(completed(shortened)));

    expect(screen.getByText(notice)).toBeTruthy();
    expect(screen.getByText('Data source')).toBeTruthy();
  });

  it('shows a refused run as no result, with the reason, the counts admission reached and the session it could not make ready', async () => {
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
    const { run } = await renderPage();

    await run((request) =>
      request.flush(refused(refusal, dataSource({ snapshot_digest: null, effective_window: null, warmup: null, coverage })), {
        status: 409,
        statusText: 'Conflict',
      }),
    );

    expect(screen.getByRole('alert').textContent).toContain('No result');
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.getByText(/11 sessions · 0 reused · 9 added · 0 missing · 1 failed \(1 unavailable from the provider\)/)).toBeTruthy();
    expect(screen.getByText('provider_no_data')).toBeTruthy();
    expect(screen.queryByText('RSI (14)')).toBeNull();
  });

  it('shows a refusal that came before anything was read with its reason and no source line', async () => {
    const reason = 'Daily bars are not read from the lake: its daily rollup folds extended-hours trades and is not the vendor\'s daily bar.';
    const refusal = { code: 'resolution_unsupported', reason, sessions: [], sessions_total: 0, coverage: null };
    const { run } = await renderPage();

    await run((request) => request.flush(refused(refusal, null), { status: 409, statusText: 'Conflict' }));

    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.queryByText('Data source')).toBeNull();
  });

  it('shows a plain-text error as it always did and no lake source line', async () => {
    const { run } = await renderPage();

    await run((request) => request.flush({ detail: 'Maximum horizon is 100 bars' }, { status: 400, statusText: 'Bad Request' }));

    expect(screen.getByText('Maximum horizon is 100 bars')).toBeTruthy();
    expect(screen.queryByText('Data source')).toBeNull();
    expect(screen.queryByText(/No result/)).toBeNull();
  });
});
