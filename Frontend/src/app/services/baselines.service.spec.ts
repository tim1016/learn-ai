import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import { BaselinesService } from './baselines.service';
import type {
  BaselineListResponse,
  BaselineResponse,
} from './baselines.types';
import type { RunLedger } from './strategy-runs.types';

const BASE_URL = `${environment.pythonServiceUrl}/api/research/strategy-runs/baselines`;

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

function makeBaselineResponse(): BaselineResponse {
  return {
    config: {
      baseline_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      parent_trade_log_hash: 't'.repeat(64),
      method: 'buy_and_hold',
      sample_count: 1,
      random_seed: 0,
      method_params: {},
      target_metrics: ['sharpe_ratio'],
      created_at_ms: 1736000000000,
    },
    result: {
      baseline_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      method: 'buy_and_hold',
      sample_count: 1,
      baselines: [],
      null_distributions: [],
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

describe('BaselinesService', () => {
  let service: BaselinesService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BaselinesService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  describe('createBaseline', () => {
    it('POSTs the request body verbatim and returns config + result', async () => {
      const promise = service.createBaseline({
        parent_run_id: 'a'.repeat(32),
        method: 'random_ema_windows',
        sample_count: 30,
        random_seed: 7,
      });
      const req = httpMock.expectOne(BASE_URL);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.method).toBe('random_ema_windows');
      expect(req.request.body.sample_count).toBe(30);
      req.flush(makeBaselineResponse());
      await promise;
    });
  });

  describe('getBaseline', () => {
    it('GETs the baseline-id-scoped URL', async () => {
      const promise = service.getBaseline('b'.repeat(32));
      const req = httpMock.expectOne(`${BASE_URL}/${'b'.repeat(32)}`);
      expect(req.request.method).toBe('GET');
      req.flush(makeBaselineResponse());
      await promise;
    });

    it('URL-encodes a malformed baseline_id', async () => {
      service.getBaseline('../escape').catch(() => {
        /* ignore */
      });
      const req = httpMock.expectOne(`${BASE_URL}/..%2Fescape`);
      req.flush({ detail: 'invalid' }, { status: 400, statusText: 'Bad Request' });
    });
  });

  describe('listBaselines', () => {
    it('GETs base URL with no params when no filters', async () => {
      const mock: BaselineListResponse = { baselines: [] };
      const promise = service.listBaselines();
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      expect(req.request.method).toBe('GET');
      expect(req.request.params.keys()).toHaveLength(0);
      req.flush(mock);
      await promise;
    });

    it('forwards every filter as a query param', async () => {
      const promise = service.listBaselines({
        parent_run_id: 'parent',
        method: 'buy_and_hold',
        since_ms: 1700000000000,
        limit: 10,
      });
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      const params = req.request.params;
      expect(params.get('parent_run_id')).toBe('parent');
      expect(params.get('method')).toBe('buy_and_hold');
      expect(params.get('since_ms')).toBe('1700000000000');
      expect(params.get('limit')).toBe('10');
      req.flush({ baselines: [] });
      await promise;
    });
  });

  describe('runFromRun', () => {
    it('buy_and_hold uses sample_count=1', async () => {
      const run = makeRunLedger({ run_id: 'c'.repeat(32) });
      const promise = service.runFromRun(run, 'buy_and_hold');
      const req = httpMock.expectOne(BASE_URL);
      const body = req.request.body;
      expect(body.parent_run_id).toBe('c'.repeat(32));
      expect(body.method).toBe('buy_and_hold');
      expect(body.sample_count).toBe(1);
      req.flush(makeBaselineResponse());
      await promise;
    });

    it('random_ema_windows defaults to sample_count=30 with workbench ranges', async () => {
      const run = makeRunLedger({ run_id: 'c'.repeat(32) });
      const promise = service.runFromRun(run, 'random_ema_windows');
      const req = httpMock.expectOne(BASE_URL);
      const body = req.request.body;
      expect(body.method).toBe('random_ema_windows');
      expect(body.sample_count).toBe(30);
      expect(body.fast_range).toEqual([3, 12]);
      expect(body.slow_range).toEqual([10, 30]);
      expect(body.random_seed).toBe(0);
      req.flush(makeBaselineResponse());
      await promise;
    });
  });
});
