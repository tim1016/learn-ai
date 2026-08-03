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

  // Stop's token is a pure function of `running` (action_policy.py:362) — a
  // single boolean. `enabled` already IS `running`, so a real "Stop, enabled"
  // action can only ever recompute to the SAME token; there is no reachable
  // backend state where it is re-offered enabled with a DIFFERENT token. The
  // two tests below cover Stop's only two real post-409 outcomes (disabled,
  // or unchanged token) and both correctly bail — Stop cannot productively
  // retry through this mechanism (see runBotAction's docstring).
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

  // `resume`'s token additionally derives from resume-admission evidence
  // (configuration_hash, evidence_refs) that can legitimately change while
  // `allowed` — and so `enabled` — stays true, so unlike Stop it CAN be
  // re-offered enabled with a different token after a 409.
  const staleResume: PanelAction = {
    action_id: 'resume',
    revision: 1,
    concurrency_token: 'tok-stale',
    enabled: true,
    label: 'Resume',
    explanation: '',
    blockers: [],
    confirmation: null,
  };

  const staleFlattenStop: PanelAction = {
    action_id: 'flatten_stop',
    revision: 1,
    concurrency_token: 'tok-stale',
    enabled: true,
    label: 'Flatten & stop',
    explanation: '',
    blockers: [],
    confirmation: {
      title: 'Flatten attributed exposure and stop?',
      body: 'Attributed exposure: AAPL 10.',
      consequence: 'The runtime stops first.',
      confirm_label: 'Flatten & stop',
      required_token: 'FLATTEN',
    },
  };

  const ACTIONS_URL = '/api/brokers/alpaca/accounts/acct-1/bots/sid-1/actions';
  const PANEL_URL = '/api/brokers/alpaca/accounts/acct-1/bots/sid-1/panel';

  const conflict = () =>
    ({ detail: { message: 'stale' } });

  it('refetches a fresh token and retries once when a transient 409 clears (unconfirmed action)', async () => {
    const promise = service.runBotAction('alpaca', 'acct-1', 'sid-1', staleResume);

    http
      .expectOne(ACTIONS_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({
      actions: [{ ...staleResume, concurrency_token: 'tok-fresh' }],
    });
    await tick();

    const retry = http.expectOne(ACTIONS_URL);
    // The retry carries the CURRENT token, not the stale one.
    expect(retry.request.body.concurrency_token).toBe('tok-fresh');
    retry.flush({
      action_id: 'resume',
      receipt_id: 'r-1',
      recorded_at_ms: 1,
      applied: true,
      revision: 2,
      concurrency_token: 'tok-fresh',
      message: 'resumed',
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

  it('does NOT retry an action that requires operator confirmation, even if its token changed and it is still enabled', async () => {
    // flatten_stop's token derives from live exposure/working-order state,
    // and its confirmation text quotes exact numbers back to the operator. A
    // silent retry after a 409 could flatten a materially different position
    // than the one the operator confirmed — so confirmed actions always
    // re-throw and let the operator re-confirm explicitly.
    const promise = service.runBotAction('alpaca', 'acct-1', 'sid-1', staleFlattenStop);

    http
      .expectOne(ACTIONS_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });

    await expect(promise).rejects.toMatchObject({ status: 409 });
    http.expectNone(PANEL_URL);
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

describe('BrokerV2PanelService reason forwarding (Task 3.2 comment parity)', () => {
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

  const ACTIONS_URL = '/api/brokers/alpaca/accounts/acct-1/bots/sid-1/actions';
  const PANEL_URL = '/api/brokers/alpaca/accounts/acct-1/bots/sid-1/panel';

  const clearHoldAction: PanelAction = {
    action_id: 'clear_hold',
    revision: 1,
    concurrency_token: 'tok-1',
    enabled: true,
    label: 'Clear hold',
    explanation: '',
    blockers: [],
    confirmation: null,
  };

  const stopAction: PanelAction = {
    action_id: 'stop',
    revision: 1,
    concurrency_token: 'tok-1',
    enabled: true,
    label: 'Stop',
    explanation: '',
    blockers: [],
    confirmation: null,
  };

  it('forwards a supplied reason into the PanelActionRequest for a mutating gate action', async () => {
    const promise = service.runBotAction(
      'alpaca',
      'acct-1',
      'sid-1',
      clearHoldAction,
      'operator typed this comment',
    );

    const request = http.expectOne(ACTIONS_URL);
    expect(request.request.body.reason).toBe('operator typed this comment');
    request.flush({
      action_id: 'clear_hold',
      receipt_id: 'r-1',
      recorded_at_ms: 1,
      applied: true,
      revision: 2,
      concurrency_token: 'tok-2',
      message: 'cleared',
    });

    await expect(promise).resolves.toMatchObject({ receipt_id: 'r-1' });
  });

  it('sends reason: null when no reason is supplied for a non-mutating action', async () => {
    const promise = service.runBotAction('alpaca', 'acct-1', 'sid-1', stopAction);

    const request = http.expectOne(ACTIONS_URL);
    expect(request.request.body.reason).toBeNull();
    request.flush({
      action_id: 'stop',
      receipt_id: 'r-2',
      recorded_at_ms: 1,
      applied: true,
      revision: 2,
      concurrency_token: 'tok-2',
      message: 'stopped',
    });

    await expect(promise).resolves.toMatchObject({ receipt_id: 'r-2' });
  });

  it('carries the supplied reason through the 409 fresh-token retry path', async () => {
    const promise = service.runBotAction(
      'alpaca',
      'acct-1',
      'sid-1',
      clearHoldAction,
      'killed mid-fill, clearing stale hold',
    );

    const first = http.expectOne(ACTIONS_URL);
    expect(first.request.body.reason).toBe('killed mid-fill, clearing stale hold');
    first.flush({ detail: { message: 'stale' } }, { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({
      actions: [{ ...clearHoldAction, concurrency_token: 'tok-fresh' }],
    });
    await tick();

    const retry = http.expectOne(ACTIONS_URL);
    expect(retry.request.body.concurrency_token).toBe('tok-fresh');
    expect(retry.request.body.reason).toBe('killed mid-fill, clearing stale hold');
    retry.flush({
      action_id: 'clear_hold',
      receipt_id: 'r-3',
      recorded_at_ms: 1,
      applied: true,
      revision: 2,
      concurrency_token: 'tok-fresh',
      message: 'cleared',
    });

    await expect(promise).resolves.toMatchObject({ receipt_id: 'r-3' });
  });
});
