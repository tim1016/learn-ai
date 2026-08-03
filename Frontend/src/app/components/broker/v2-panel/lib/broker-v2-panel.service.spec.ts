import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BrokerV2PanelService } from './broker-v2-panel.service';
import type { PanelAction } from './broker-v2-panel.types';

describe('BrokerV2PanelService run evidence', () => {
  let service: BrokerV2PanelService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerV2PanelService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads the current run from the account-independent run endpoint', async () => {
    const response = service.getCurrentRun('alpaca paper', 'sid/001');
    const request = http.expectOne(
      '/api/brokers/alpaca%20paper/bots/sid%2F001/runs/current',
    );
    expect(request.request.method).toBe('GET');
    request.flush({
      strategy_instance_id: 'sid/001',
      run_id: 'run-current',
      configuration_hash: 'a'.repeat(64),
      launch_reason: 'deploy',
      started_at_ms: 1_753_800_000_000,
      is_current: true,
      process: null,
      terminal_outcome: null,
    });

    await expect(response).resolves.toMatchObject({ run_id: 'run-current' });
  });

  it('loads exactly one previous run using the opaque server cursor', async () => {
    const response = service.getRunHistory('alpaca', 'sid-001', 'run/newest');
    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/brokers/alpaca/bots/sid-001/runs/history' &&
        candidate.params.get('limit') === '1' &&
        candidate.params.get('cursor') === 'run/newest',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ runs: [], next_cursor: null });

    await expect(response).resolves.toEqual({ runs: [], next_cursor: null });
  });
});

describe('BrokerV2PanelService resilient action retry (defect #10)', () => {
  let service: BrokerV2PanelService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerV2PanelService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

  const staleStop: PanelAction = {
    action_id: 'stop',
    revision: 1,
    concurrency_token: 'tok-stale',
    enabled: true,
    label: 'Stop',
    explanation: '',
    blockers: [],
    confirmation: null,
  };

  const ACTIONS_URL = '/api/brokers/alpaca/accounts/acct-1/bots/sid-1/actions';
  const PANEL_URL = '/api/brokers/alpaca/accounts/acct-1/bots/sid-1/panel';

  const conflict = () =>
    ({ detail: { message: 'stale' } });

  it('refetches a fresh token and retries once when a transient 409 clears', async () => {
    const promise = service.runBotAction('alpaca', 'acct-1', 'sid-1', staleStop);

    http
      .expectOne(ACTIONS_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({
      actions: [{ ...staleStop, concurrency_token: 'tok-fresh' }],
    });
    await tick();

    const retry = http.expectOne(ACTIONS_URL);
    // The retry carries the CURRENT token, not the stale one.
    expect(retry.request.body.concurrency_token).toBe('tok-fresh');
    retry.flush({
      action_id: 'stop',
      receipt_id: 'r-1',
      recorded_at_ms: 1,
      applied: true,
      revision: 2,
      concurrency_token: 'tok-fresh',
      message: 'stopped',
    });

    await expect(promise).resolves.toMatchObject({ receipt_id: 'r-1' });
  });

  it('does NOT retry when the action is disabled after the 409 (state truly changed)', async () => {
    const promise = service.runBotAction('alpaca', 'acct-1', 'sid-1', staleStop);

    http
      .expectOne(ACTIONS_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({
      actions: [{ ...staleStop, concurrency_token: 'tok-fresh', enabled: false }],
    });

    await expect(promise).rejects.toMatchObject({ status: 409 });
  });

  it('does NOT retry when the fresh token is unchanged (an availability 409)', async () => {
    const promise = service.runBotAction('alpaca', 'acct-1', 'sid-1', staleStop);

    http
      .expectOne(ACTIONS_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({ actions: [staleStop] });

    await expect(promise).rejects.toMatchObject({ status: 409 });
  });

  it('re-throws a non-409 error without refetching the panel', async () => {
    const promise = service.runBotAction('alpaca', 'acct-1', 'sid-1', staleStop);

    http
      .expectOne(ACTIONS_URL)
      .flush({ detail: { message: 'boom' } }, { status: 500, statusText: 'Server Error' });

    await expect(promise).rejects.toMatchObject({ status: 500 });
    http.expectNone(PANEL_URL);
  });
});
