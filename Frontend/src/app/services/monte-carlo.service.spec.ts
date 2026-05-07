import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import { MonteCarloService } from './monte-carlo.service';
import type {
  MonteCarloListResponse,
  MonteCarloResponse,
} from './monte-carlo.types';
import type { RunLedger } from './strategy-runs.types';

const BASE_URL = `${environment.pythonServiceUrl}/api/research/strategy-runs/monte-carlo`;

function makeRunLedger(overrides: Partial<RunLedger> = {}): RunLedger {
  return {
    schema_version: '1.0',
    run_id: 'a'.repeat(32),
    parent_run_id: null,
    parent_spec_hash: null,
    strategy_spec_id: 'spy_ema_crossover',
    strategy_spec_hash: 'd'.repeat(64),
    strategy_spec_json: { name: 'spy_ema_crossover' },
    engine_name: 'learn_ai_event_driven',
    engine_version: '0.1.0',
    engine_git_commit: 'unknown',
    symbol: 'SPY',
    resolution_minutes: 15,
    start_ms: 1704171600000,
    end_ms: 1735621200000,
    initial_cash: 100000,
    fill_mode: 'signal_bar_close',
    commission_per_order: 0,
    slippage_per_share: 0,
    warmup_policy: 'spec_indicator_warmup',
    random_seed: 0,
    data_source: 'lean_minute_reader',
    data_snapshot_id: 'SPY|15|...|test',
    result_hash: 'r'.repeat(64),
    trade_log_hash: 't'.repeat(64),
    metrics_hash: 'm'.repeat(64),
    created_at_ms: 1736000000000,
    completed_at_ms: 1736000001000,
    status: 'completed',
    failure_reason: null,
    ...overrides,
  };
}

function makeMcResponse(): MonteCarloResponse {
  return {
    config: {
      monte_carlo_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      parent_trade_log_hash: 't'.repeat(64),
      method: 'reshuffle',
      simulation_count: 1000,
      projection_trade_count: 0,
      initial_equity: 100000,
      random_seed: 0,
      breach_thresholds: [0.05, 0.10],
      created_at_ms: 1736000000000,
    },
    result: {
      monte_carlo_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      method: 'reshuffle',
      simulation_count: 1000,
      realised_trade_count: 25,
      equity_bands: [],
      drawdown_quantiles: { p5: 0.01, p50: 0.05, p95: 0.12 },
      terminal_pnl_quantiles: { p5: -100, p50: 500, p95: 1500 },
      max_losing_streak_quantiles: { p5: 1, p50: 2, p95: 4 },
      breach_probabilities: [],
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

describe('MonteCarloService', () => {
  let service: MonteCarloService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(MonteCarloService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  describe('createMonteCarlo', () => {
    it('POSTs the request body verbatim and returns config + result', async () => {
      const promise = service.createMonteCarlo({
        parent_run_id: 'a'.repeat(32),
        method: 'reshuffle',
        simulation_count: 500,
        breach_thresholds: [0.1],
      });
      const req = httpMock.expectOne(BASE_URL);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.method).toBe('reshuffle');
      expect(req.request.body.simulation_count).toBe(500);
      req.flush(makeMcResponse());
      const result = await promise;
      expect(result.config.monte_carlo_id).toHaveLength(32);
    });
  });

  describe('getMonteCarlo', () => {
    it('GETs the mc-id-scoped URL', async () => {
      const promise = service.getMonteCarlo('b'.repeat(32));
      const req = httpMock.expectOne(`${BASE_URL}/${'b'.repeat(32)}`);
      expect(req.request.method).toBe('GET');
      req.flush(makeMcResponse());
      const out = await promise;
      expect(out.config.monte_carlo_id).toBe('b'.repeat(32));
    });

    it('URL-encodes a malformed mc_id', async () => {
      service.getMonteCarlo('../escape').catch(() => {
        /* ignore */
      });
      const req = httpMock.expectOne(`${BASE_URL}/..%2Fescape`);
      req.flush({ detail: 'invalid' }, { status: 400, statusText: 'Bad Request' });
    });
  });

  describe('listMonteCarlos', () => {
    it('GETs base URL with no params when no filters', async () => {
      const mock: MonteCarloListResponse = { monte_carlos: [] };
      const promise = service.listMonteCarlos();
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      expect(req.request.method).toBe('GET');
      expect(req.request.params.keys()).toHaveLength(0);
      req.flush(mock);
      await promise;
    });

    it('forwards every filter as a query param', async () => {
      const promise = service.listMonteCarlos({
        parent_run_id: 'parent',
        method: 'resample',
        since_ms: 1700000000000,
        limit: 10,
      });
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      const params = req.request.params;
      expect(params.get('parent_run_id')).toBe('parent');
      expect(params.get('method')).toBe('resample');
      expect(params.get('since_ms')).toBe('1700000000000');
      expect(params.get('limit')).toBe('10');
      req.flush({ monte_carlos: [] });
      await promise;
    });
  });

  describe('runReshuffleFromRun', () => {
    it('derives a 1000-sim reshuffle MC linked to the parent run', async () => {
      const run = makeRunLedger({ run_id: 'c'.repeat(32) });
      const promise = service.runReshuffleFromRun(run);
      const req = httpMock.expectOne(BASE_URL);
      expect(req.request.method).toBe('POST');

      const body = req.request.body;
      expect(body.parent_run_id).toBe('c'.repeat(32));
      expect(body.method).toBe('reshuffle');
      expect(body.simulation_count).toBe(1000);
      expect(body.random_seed).toBe(0);
      // Workbench-default breach thresholds.
      expect(body.breach_thresholds).toEqual([0.05, 0.10, 0.20, 0.30]);

      req.flush(makeMcResponse());
      await promise;
    });
  });
});
