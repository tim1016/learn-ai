import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import completedRun from '@repo-contracts/fixtures/spec-strategy-backtest-response-v1.json';

import { environment } from '../../environments/environment';
import { SpecStrategyBacktestResult, StrategySpec } from '../graphql/spec-strategy.models';
import { SpecStrategyService } from './spec-strategy.service';

const URL = `${environment.pythonServiceUrl}/api/spec-strategy/backtest`;

/** The committed cross-stack wire example, typed as the generated response contract. */
const SUCCESS_BODY: SpecStrategyBacktestResult = completedRun;

const TRIVIAL_SPEC: StrategySpec = {
  schema_version: '1.0',
  name: 'spec-service-test',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [
    { id: 'sma_s', kind: 'SMA', period: 5 },
    { id: 'sma_l', kind: 'SMA', period: 10 },
  ],
  entry: {
    logic: 'AND',
    conditions: [{ kind: 'FreshCross', left: 'sma_s', right: 'sma_l', direction: 'up' }],
    size: { kind: 'SetHoldings', fraction: 1.0 },
  },
  exit: { logic: 'OR', conditions: [] },
};

const RUN_WINDOW = { startDate: '2024-01-02', endDate: '2024-12-31' };

describe('SpecStrategyService', () => {
  let service: SpecStrategyService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(SpecStrategyService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('POSTs the spec as an object with snake_case run params to the Python backtest endpoint', async () => {
    const pending = service.runBacktest(TRIVIAL_SPEC, {
      ...RUN_WINDOW,
      initialCash: 50000,
      fillMode: 'next_bar_open',
      commissionPerOrder: 1,
    });

    const req = http.expectOne(URL);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      spec: TRIVIAL_SPEC,
      start_date: '2024-01-02',
      end_date: '2024-12-31',
      initial_cash: 50000,
      fill_mode: 'next_bar_open',
      commission_per_order: 1,
    });
    req.flush(SUCCESS_BODY);

    const result = await pending;
    expect(result).toEqual(SUCCESS_BODY);
    // The wire shape reaches callers untouched: int64-ms timestamps stay
    // numbers and the indicator snapshot stays Python's dict.
    const trade = result.trades?.[0];
    expect(trade?.entry_time).toBe(1704153600000);
    expect(trade?.exit_time).toBe(1704157200000);
    expect(trade?.indicators).toEqual({ ema_fast: 471.0, ema_slow: 470.2 });
  });

  it("applies Python's own defaults for the optional run params", async () => {
    const pending = service.runBacktest(TRIVIAL_SPEC, RUN_WINDOW);

    const req = http.expectOne(URL);
    expect(req.request.body).toMatchObject({
      initial_cash: 100000,
      fill_mode: 'signal_bar_close',
      commission_per_order: 0,
    });
    req.flush(SUCCESS_BODY);

    await pending;
  });

  it('exposes loading, result and a clear error via signals', async () => {
    expect(service.loading()).toBe(false);
    expect(service.result()).toBeNull();

    const pending = service.runBacktest(TRIVIAL_SPEC, RUN_WINDOW);
    expect(service.loading()).toBe(true);

    http.expectOne(URL).flush(SUCCESS_BODY);
    await pending;

    expect(service.loading()).toBe(false);
    expect(service.result()?.strategy_name).toBe('EMA crossover');
    expect(service.error()).toBeNull();
  });

  it('resolves a completed backtest that reports success=false and mirrors its error', async () => {
    const pending = service.runBacktest(TRIVIAL_SPEC, RUN_WINDOW);
    http.expectOne(URL).flush({
      ...SUCCESS_BODY,
      success: false,
      total_trades: 0,
      trades: [],
      error: 'spec uses unsupported feature: option template',
    });

    const result = await pending;
    expect(result.success).toBe(false);
    expect(result.error).toContain('option template');
    expect(service.result()).toEqual(result);
    expect(service.error()).toContain('option template');
  });

  it('rejects an HTTP failure and surfaces the FastAPI detail as the error', async () => {
    const pending = service.runBacktest(TRIVIAL_SPEC, RUN_WINDOW);
    http
      .expectOne(URL)
      .flush({ detail: "start_date must be YYYY-MM-DD: '2024/01/02'" }, { status: 400, statusText: 'Bad Request' });

    await expect(pending).rejects.toBeInstanceOf(HttpErrorResponse);
    expect(service.error()).toBe("start_date must be YYYY-MM-DD: '2024/01/02'");
    expect(service.result()).toBeNull();
    expect(service.loading()).toBe(false);
  });

  it('falls back to the HTTP status line when the failure body carries no detail', async () => {
    const pending = service.runBacktest(TRIVIAL_SPEC, RUN_WINDOW);
    http.expectOne(URL).flush('upstream exploded', { status: 502, statusText: 'Bad Gateway' });

    await expect(pending).rejects.toBeInstanceOf(HttpErrorResponse);
    expect(service.error()).toContain('502');
  });

  it('reset clears the last result and error', async () => {
    const pending = service.runBacktest(TRIVIAL_SPEC, RUN_WINDOW);
    http.expectOne(URL).flush({ ...SUCCESS_BODY, success: false, error: 'boom' });
    await pending;
    expect(service.result()).not.toBeNull();
    expect(service.error()).toBe('boom');

    service.reset();

    expect(service.result()).toBeNull();
    expect(service.error()).toBeNull();
  });
});
