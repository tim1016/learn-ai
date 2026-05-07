import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import type { RunLedger } from './strategy-runs.types';
import { WalkForwardService } from './walk-forward.service';
import type {
  WalkForwardListResponse,
  WalkForwardResponse,
} from './walk-forward.types';

const BASE_URL = `${environment.pythonServiceUrl}/api/research/strategy-runs/walk-forward`;

function makeRunLedger(overrides: Partial<RunLedger> = {}): RunLedger {
  return {
    schema_version: '1.0',
    run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    parent_run_id: null,
    parent_spec_hash: null,
    strategy_spec_id: 'spy_ema_crossover',
    strategy_spec_hash: 'd'.repeat(64),
    strategy_spec_json: { name: 'spy_ema_crossover', symbols: ['SPY'] },
    engine_name: 'learn_ai_event_driven',
    engine_version: '0.1.0',
    engine_git_commit: 'unknown',
    symbol: 'SPY',
    resolution_minutes: 15,
    // NY-midnight UTC ms (matches Phase A's _date_to_ny_midnight_ms output).
    // 2024-01-02 NY-midnight = 2024-01-02T05:00:00Z = 1704171600000.
    // 2024-12-31 NY-midnight = 2024-12-31T05:00:00Z = 1735621200000.
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

function makeWfResponse(): WalkForwardResponse {
  return {
    config: {
      walk_forward_id: 'b'.repeat(32),
      parent_run_id: null,
      strategy_spec_hash: 'd'.repeat(64),
      strategy_spec_json: { name: 'test' },
      symbol: 'SPY',
      resolution_minutes: 15,
      start_ms: 1704171600000,
      end_ms: 1735621200000,
      initial_cash: 100000,
      fill_mode: 'signal_bar_close',
      commission_per_order: 0,
      slippage_per_share: 0,
      random_seed: 0,
      split_policy: {
        kind: 'rolling',
        train_days: 60,
        test_days: 30,
        step_days: 30,
      },
      created_at_ms: 1736000000000,
    },
    result: {
      walk_forward_id: 'b'.repeat(32),
      parent_run_id: null,
      strategy_spec_hash: 'd'.repeat(64),
      split_policy: {
        kind: 'rolling',
        train_days: 60,
        test_days: 30,
        step_days: 30,
      },
      folds: [],
      combined_oos_equity_curve: [],
      mean_oos_sharpe: null,
      median_oos_sharpe: null,
      pct_profitable_folds: null,
      oos_retention: null,
      alpha_decay: null,
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

describe('WalkForwardService', () => {
  let service: WalkForwardService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(WalkForwardService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  describe('createWalkForward', () => {
    it('POSTs the request body verbatim and returns config + result', async () => {
      const promise = service.createWalkForward({
        spec: { schema_version: '1.0', name: 'inline' },
        start_date: '2024-01-02',
        end_date: '2024-12-31',
        split_policy: { kind: 'chronological', train_pct: 0.7 },
      });
      const req = httpMock.expectOne(BASE_URL);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.start_date).toBe('2024-01-02');
      expect(req.request.body.split_policy.kind).toBe('chronological');
      req.flush(makeWfResponse());
      const result = await promise;
      expect(result.config.walk_forward_id).toHaveLength(32);
    });
  });

  describe('getWalkForward', () => {
    it('GETs the wf-id-scoped URL', async () => {
      const promise = service.getWalkForward('b'.repeat(32));
      const req = httpMock.expectOne(`${BASE_URL}/${'b'.repeat(32)}`);
      expect(req.request.method).toBe('GET');
      req.flush(makeWfResponse());
      const out = await promise;
      expect(out.config.walk_forward_id).toBe('b'.repeat(32));
    });

    it('URL-encodes a malformed wf_id', async () => {
      service.getWalkForward('../escape').catch(() => {
        /* ignore */
      });
      const req = httpMock.expectOne(`${BASE_URL}/..%2Fescape`);
      req.flush({ detail: 'invalid' }, { status: 400, statusText: 'Bad Request' });
    });
  });

  describe('listWalkForwards', () => {
    it('GETs base URL with no params when no filters', async () => {
      const mock: WalkForwardListResponse = { walk_forwards: [] };
      const promise = service.listWalkForwards();
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      expect(req.request.method).toBe('GET');
      expect(req.request.params.keys()).toHaveLength(0);
      req.flush(mock);
      await promise;
    });

    it('forwards every filter as a query param', async () => {
      const promise = service.listWalkForwards({
        parent_run_id: 'parent',
        spec_hash: 'hash',
        since_ms: 1700000000000,
        limit: 10,
      });
      const req = httpMock.expectOne((r) => r.url === BASE_URL);
      const params = req.request.params;
      expect(params.get('parent_run_id')).toBe('parent');
      expect(params.get('spec_hash')).toBe('hash');
      expect(params.get('since_ms')).toBe('1700000000000');
      expect(params.get('limit')).toBe('10');
      req.flush({ walk_forwards: [] });
      await promise;
    });
  });

  describe('runFromRun', () => {
    it('derives a rolling 60/30/30 WF from a run ledger and links parent_run_id', async () => {
      const run = makeRunLedger({ run_id: 'c'.repeat(32) });
      const promise = service.runFromRun(run);
      const req = httpMock.expectOne(BASE_URL);
      expect(req.request.method).toBe('POST');

      const body = req.request.body;
      // Spec is the run's spec JSON, not its hash.
      expect(body.spec).toEqual(run.strategy_spec_json);
      // Window converts run's int64 ms boundaries to YYYY-MM-DD.
      expect(body.start_date).toBe('2024-01-02');
      expect(body.end_date).toBe('2024-12-31');
      // Hard-coded default split for v1.
      expect(body.split_policy).toEqual({
        kind: 'rolling',
        train_days: 60,
        test_days: 30,
        step_days: 30,
      });
      // Cost model is copied through.
      expect(body.fill_mode).toBe(run.fill_mode);
      expect(body.commission_per_order).toBe(run.commission_per_order);
      expect(body.slippage_per_share).toBe(run.slippage_per_share);
      // Parent linkage is set.
      expect(body.parent_run_id).toBe('c'.repeat(32));

      req.flush(makeWfResponse());
      await promise;
    });
  });
});
