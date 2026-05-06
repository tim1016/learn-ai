import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { StrategyRunsService } from './strategy-runs.service';
import type {
  StrategyRunListResponse,
  StrategyRunResponse,
} from './strategy-runs.types';
import { environment } from '../../environments/environment';

const BASE_URL = `${environment.pythonServiceUrl}/api/research/strategy-runs`;

function makeLedger(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: '1.0' as const,
    run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    parent_run_id: null,
    parent_spec_hash: null,
    strategy_spec_id: 'spy_ema_crossover',
    strategy_spec_hash: 'deadbeef'.repeat(8),
    strategy_spec_json: { name: 'spy_ema_crossover' },
    engine_name: 'learn_ai_event_driven' as const,
    engine_version: '0.1.0',
    engine_git_commit: 'unknown',
    symbol: 'SPY',
    resolution_minutes: 15,
    start_ms: 1704160800000,
    end_ms: 1735714800000,
    initial_cash: 100000,
    fill_mode: 'signal_bar_close',
    commission_per_order: 0,
    slippage_per_share: 0,
    warmup_policy: 'spec_indicator_warmup' as const,
    random_seed: 0,
    data_source: 'lean_minute_reader' as const,
    data_snapshot_id: 'SPY|15|1704160800000|1735714800000|test',
    result_hash: 'r'.repeat(64),
    trade_log_hash: 't'.repeat(64),
    metrics_hash: 'm'.repeat(64),
    created_at_ms: 1736000000000,
    completed_at_ms: 1736000001000,
    status: 'completed' as const,
    failure_reason: null,
    ...overrides,
  };
}

function makeRunResponse(): StrategyRunResponse {
  return {
    ledger: makeLedger(),
    result: {
      run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      initial_cash: 100000,
      final_equity: 105000,
      equity_curve: [{ timestamp_ms: 1704160800000, equity: 100000 }],
      drawdown_curve: [{ timestamp_ms: 1704160800000, drawdown_pct: 0 }],
      trades: [],
      metrics: {
        total_trades: 0,
        winning_trades: 0,
        losing_trades: 0,
        win_rate: null,
        total_return_pct: 0.05,
        max_drawdown_pct: 0,
        sharpe_ratio: null,
        sortino_ratio: null,
        profit_factor: null,
        expectancy_pct: null,
        payoff_ratio: null,
        exposure_pct: null,
        avg_trade_bars: null,
      },
      log_lines: [],
      warnings: [],
    },
  };
}

describe('StrategyRunsService', () => {
  let service: StrategyRunsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(StrategyRunsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  describe('listRuns', () => {
    it('GETs the base URL with no params when no filters are passed', async () => {
      const mock: StrategyRunListResponse = { runs: [makeLedger()] };

      const promise = service.listRuns();
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      expect(req.request.method).toBe('GET');
      expect(req.request.params.keys()).toHaveLength(0);
      req.flush(mock);

      const result = await promise;
      expect(result.runs).toHaveLength(1);
      expect(result.runs[0]?.symbol).toBe('SPY');
    });

    it('passes every filter through as a query param', async () => {
      const promise = service.listRuns({
        spec_hash: 'abc',
        symbol: 'SPY',
        status: 'completed',
        parent_run_id: 'parent',
        parent_spec_hash: 'pspec',
        since_ms: 1700000000000,
        limit: 50,
      });
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      const params = req.request.params;
      expect(params.get('spec_hash')).toBe('abc');
      expect(params.get('symbol')).toBe('SPY');
      expect(params.get('status')).toBe('completed');
      expect(params.get('parent_run_id')).toBe('parent');
      expect(params.get('parent_spec_hash')).toBe('pspec');
      expect(params.get('since_ms')).toBe('1700000000000');
      expect(params.get('limit')).toBe('50');
      req.flush({ runs: [] });
      await promise;
    });

    it('omits filters whose value is undefined or empty', async () => {
      const promise = service.listRuns({ symbol: 'SPY' });
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      expect(req.request.params.keys()).toEqual(['symbol']);
      req.flush({ runs: [] });
      await promise;
    });
  });

  describe('getRun', () => {
    it('GETs the run-id-scoped URL', async () => {
      const promise = service.getRun('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
      const req = httpMock.expectOne(
        `${BASE_URL}/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`,
      );
      expect(req.request.method).toBe('GET');
      req.flush(makeRunResponse());
      const result = await promise;
      expect(result.ledger.run_id).toBe('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
    });

    it('URL-encodes the run_id (defense against unsanitized input even though the server validates)', async () => {
      // The service must not let "../escape" reach the wire un-encoded —
      // server-side rejects the format anyway, but encoding it client-side
      // means a 400 with a clean URL rather than a Starlette path-routing
      // surprise.
      service.getRun('../escape').catch(() => {
        /* ignore */
      });
      const req = httpMock.expectOne(`${BASE_URL}/..%2Fescape`);
      req.flush({ detail: 'invalid' }, { status: 400, statusText: 'Bad Request' });
    });
  });

  describe('createRun', () => {
    it('POSTs the request body verbatim and returns ledger + result', async () => {
      const promise = service.createRun({
        spec: { schema_version: '1.0', name: 'inline-test' },
        start_date: '2024-01-02',
        end_date: '2024-12-31',
      });
      const req = httpMock.expectOne(BASE_URL);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.start_date).toBe('2024-01-02');
      expect(req.request.body.spec.name).toBe('inline-test');
      req.flush(makeRunResponse());
      const result = await promise;
      expect(result.ledger.status).toBe('completed');
    });
  });

  describe('runSpyEmaFixture', () => {
    it('fetches the canonical SPY EMA fixture spec, then submits it', async () => {
      const promise = service.runSpyEmaFixture();

      // First the fixture GET.
      const fixtureUrl = `${environment.pythonServiceUrl}/api/spec-strategy/fixtures/spy_ema_crossover`;
      const fixtureReq = httpMock.expectOne(fixtureUrl);
      expect(fixtureReq.request.method).toBe('GET');
      const spec = { schema_version: '1.0', name: 'SPY EMA Crossover', symbols: ['SPY'] };
      fixtureReq.flush(spec);

      // Yield to the microtask queue so the awaited fixture promise
      // resolves and the chained POST is actually issued before
      // ``expectOne`` polls the testing backend. Without this yield
      // the POST hasn't fired yet and the assertion finds zero matches.
      await Promise.resolve();

      // Then the POST with that spec inlined.
      const postReq = httpMock.expectOne(BASE_URL);
      expect(postReq.request.method).toBe('POST');
      expect(postReq.request.body.spec).toEqual(spec);
      expect(postReq.request.body.start_date).toBe('2024-01-02');
      expect(postReq.request.body.end_date).toBe('2024-12-31');
      expect(postReq.request.body.strategy_spec_id).toBe('spy_ema_crossover');
      postReq.flush(makeRunResponse());

      const result = await promise;
      expect(result.ledger.run_id).toHaveLength(32);
    });
  });
});
