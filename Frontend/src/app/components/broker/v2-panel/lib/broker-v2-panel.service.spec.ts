import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BrokerV2PanelService } from './broker-v2-panel.service';
import { resourceTarget } from '../../../../fleet/resource-target';
import { POLL_REQUEST_TIMEOUT_MS } from '../../../../services/poll-timeout';
import type { PanelAction, PanelActionRequest } from './broker-v2-panel.types';
import { provideFleetDirectory } from '../../../../fleet/fleet-directory-testing';
import { fakeSqliteStopAction } from '../../../../testing/bot-panel-fixtures';

const CLERK = 'clrk_spec';
const target = (accountId: string, entityId?: string) =>
  resourceTarget('alpaca', CLERK, {
    accountId,
    entityId,
    capability: 'bot_action',
    idempotencyKey: 'command-key-1',
    bindingGeneration: 3,
    routingEpoch: 4,
  });

describe('BrokerV2PanelService run evidence', () => {
  let service: BrokerV2PanelService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
      provideFleetDirectory(),provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerV2PanelService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('routes a read-only budget preview and preserves the consent string in the command envelope', async () => {
    const body = {
      strategy_key: 'deployment_validation' as const, symbol: 'SPY', execution_mode: 'paper' as const,
      sizing: { preset: 'safe_canary' as const, quantity: 1 }, carryover_policy: 'FORBID' as const,
      exit_terms: { exit_allowance_bps: 10, band_multiple: 2, spread_cap_bps: 10 },
      budget: { amount_usd: '1234.56', risk_revision: 7, review_token: 'review-proof' },
    };
    const preview = service.previewBudget(target('PA9'), body);
    const read = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/budget-preview');
    expect(read.request.body).toEqual(body);
    read.flush({ state: 'ready', review_token: 'review-proof' });
    await preview;
    const command = service.deployBudgetBot(target('PA9'), { ...body, submission_key: 'submission-key-1' });
    const write = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots');
    expect(write.request.body.budget).toEqual(body.budget);
    expect(write.request.body.submission_key).toBe('submission-key-1');
    expect(write.request.body.strategy_instance_id).toBeUndefined();
    expect(write.request.body.command_context.idempotency_key).toBe('command-key-1');
    write.flush({ status: 'pending', committed_usd: '1234.56' });
    await expect(command).resolves.toMatchObject({ status: 'pending', committed_usd: '1234.56' });
  });

  it('checks a Deploy’s end as a read, and changes a bot’s end as an enveloped PUT (#2607)', async () => {
    const end = { end_at_ms: 1_790_020_740_000, end_action: 'KEEP' as const };
    const preview = service.previewBotEnd(target('PA9'), { execution_mode: 'paper', end });
    const check = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/end-preview');
    expect(check.request.method).toBe('POST');
    expect(check.request.body).toEqual({ execution_mode: 'paper', end });
    check.flush({ headline: 'Ends today 15:59 ET · keeps its shares' });
    await expect(preview).resolves.toMatchObject({ headline: 'Ends today 15:59 ET · keeps its shares' });

    const change = service.editBotEnd(target('PA9', 'sid/1'), 'sid/1', end);
    const put = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/sid%2F1/end');
    expect(put.request.method).toBe('PUT');
    expect(put.request.body).toMatchObject(end);
    expect(put.request.body.command_context).toMatchObject({
      capability: 'bot_action', idempotency_key: 'command-key-1', expected_effective_binding_generation: 3,
    });
    put.flush({ headline: 'Ends today 15:59 ET · keeps its shares' });
    await expect(change).resolves.toMatchObject({ headline: 'Ends today 15:59 ET · keeps its shares' });
  });

  it('reads budget and durable deployment status through the selected account and escaped bot identity', async () => {
    const budget = service.getBudget(target('PA9'), 'sid/1');
    const money = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/sid%2F1/budget');
    expect(money.request.method).toBe('GET');
    money.flush({ state: 'unavailable', free_usd: null });
    await expect(budget).resolves.toMatchObject({ free_usd: null });
    const command = service.getDeploySubmission(target('PA9'), 'key_1-abc');
    const status = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/deploy-submissions/key_1-abc');
    expect(status.request.method).toBe('GET');
    status.flush({ status: 'pending' });
    await expect(command).resolves.toMatchObject({ status: 'pending' });
  });

  it('reads Deploy again settings for one earlier bot through its escaped identity', async () => {
    const prefill = service.getDeployPrefill(target('PA9'), 'sid/1');
    const read = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/sid%2F1/deploy-prefill');
    expect(read.request.method).toBe('GET');
    read.flush({ source_strategy_instance_id: 'sid/1', strategy_key: 'ema', symbol: 'SPY', sizing: {}, parameters: {} });
    await expect(prefill).resolves.toMatchObject({ source_strategy_instance_id: 'sid/1' });
  });

  it('loads the current run from the clerk-scoped run endpoint', async () => {
    const response = service.getCurrentRun(
      resourceTarget('alpaca paper', CLERK, { accountId: 'account/1', entityId: 'sid/001' }),
      'sid/001',
    );
    const request = http.expectOne(
      '/api/brokers/alpaca%20paper/clerks/clrk_spec/accounts/account%2F1/bots/sid%2F001/runs/current',
    );
    expect(request.request.method).toBe('GET');
    request.flush({
      strategy_instance_id: 'sid/001',
      run_id: 'run-current',
      configuration_hash: 'a'.repeat(64),
      launch_reason: 'deploy',
      started_at_ms: 1_753_800_000_000,
      process: {
        strategy_instance_id: 'sid/001',
        run_id: 'run-current',
        process_identity: null,
        state: 'UNKNOWN',
        registry_generation: 'registry-1',
        observed_at_ms: 1_753_800_000_000,
      },
      terminal_outcome: null,
    });

    await expect(response).resolves.toMatchObject({ run_id: 'run-current' });
  });

  it('keeps historical exact-execution recovery outside the generic panel-action endpoint', async () => {
    const prepared = service.prepareHistoricalExecutionRecovery(target('account/1', 'bot/1'), 'bot/1', 'token-1');
    const prepareRequest = http.expectOne(
      '/api/brokers/alpaca/clerks/clrk_spec/accounts/account%2F1/custody/bots/bot%2F1/historical-execution-recovery/prepare',
    );
    expect(prepareRequest.request.body).toMatchObject({
      concurrency_token: 'token-1',
      command_context: expect.objectContaining({
        capability: 'custody_command',
        expected_effective_binding_generation: 3,
      }),
    });
    prepareRequest.flush({
      account_id: 'account/1', strategy_instance_id: 'bot/1', uncertainty_id: 'u-1',
      order_ref: 'order-1', broker_order_id: 'broker-order-1', execution_id: 'execution-1',
      exact_symbol: 'SPY', exact_quantity: 1, exact_price: 100, exact_side: 'BUY',
      source_event_at_ms: 1, cumulative_fill_id: 'fill-1', cumulative_quantity: 1,
      cumulative_price: 100, cumulative_side: 'BUY', authority_generation: 1,
      db_identity_token: 'db-1', control_revision: 1, prepared_at_ms: 1,
      expires_at_ms: 2, confirmation_token: 'confirm-1',
    });
    const plan = await prepared;

    const confirmed = service.confirmHistoricalExecutionRecovery(target('account/1', 'bot/1'), 'bot/1', plan);
    const confirmRequest = http.expectOne(
      '/api/brokers/alpaca/clerks/clrk_spec/accounts/account%2F1/custody/bots/bot%2F1/historical-execution-recovery/confirm',
    );
    expect(confirmRequest.request.body).toMatchObject({
      plan,
      confirmation_token: 'confirm-1',
      command_context: expect.objectContaining({ capability: 'custody_command' }),
    });
    confirmRequest.flush({
      uncertainty_id: 'u-1', order_ref: 'order-1', execution_id: 'execution-1',
      receipt_id: 'coverage-resolution:2', recorded_at_ms: 2, applied: true,
    });

    await expect(confirmed).resolves.toMatchObject({ receipt_id: 'coverage-resolution:2' });
  });

  it('sends an extended-hours flatten with its confirmed limit to the bot custody route (#2007)', async () => {
    const sent = service.executeExtendedSafeFlatten(
      target('account/1', 'bot/1'),
      'bot/1',
      'execute-token-1',
      { limit_price: 511.28, quote_observed_at_ms: 1_753_794_000_000 },
    );
    const request = http.expectOne(
      '/api/brokers/alpaca/clerks/clrk_spec/accounts/account%2F1/custody/bots/bot%2F1/recovery-actions/execute',
    );
    expect(request.request.body).toMatchObject({
      action_id: 'execute_safe_flatten',
      concurrency_token: 'execute-token-1',
      extended_limit: { limit_price: 511.28, quote_observed_at_ms: 1_753_794_000_000 },
      command_context: expect.objectContaining({ capability: 'custody_command' }),
    });
    request.flush({
      action_id: 'execute_safe_flatten', outcome: 'success', applied: true,
      receipt_id: 'order-1', recorded_at_ms: 3, command: null, reconciliation: null, orders: [],
    });

    await expect(sent).resolves.toMatchObject({ receipt_id: 'order-1', applied: true });
  });

  it('reads the cohort-flatten presentation from the clerk-scoped roster route (ADR 0051)', async () => {
    const response = service.getCohortFlattenView(
      resourceTarget('alpaca', CLERK, { accountId: 'account/1', bindingGeneration: 3 }),
    );
    const request = http.expectOne(
      '/api/brokers/alpaca/clerks/clrk_spec/accounts/account%2F1/bots/cohort-flatten',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ account_id: 'account/1', cohorts: [], observed_at_ms: 1 });

    await expect(response).resolves.toMatchObject({ account_id: 'account/1', cohorts: [] });
  });

  it('posts exactly the confirmed flatten legs under the frozen durable key (ADR 0051)', async () => {
    const legs = [
      {
        strategy_instance_id: 'qqq-1',
        action_id: 'execute_safe_flatten' as const,
        revision: 7,
        concurrency_token: 'tok-1',
      },
    ];
    const sent = service.runCohortFlatten(target('account/1'), {
      idempotency_key: 'command-key-1',
      reason: 'Cohort flatten from the bots roster',
      legs,
    });
    const request = http.expectOne(
      '/api/brokers/alpaca/clerks/clrk_spec/accounts/account%2F1/bots/cohort-flatten',
    );
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toMatchObject({
      idempotency_key: 'command-key-1',
      legs,
      command_context: expect.objectContaining({
        capability: 'bot_action',
        idempotency_key: 'command-key-1',
        expected_effective_binding_generation: 3,
      }),
    });
    request.flush({
      account_id: 'account/1', receipt_id: 'command-key-1', recorded_at_ms: 2, legs: [],
      applied_count: 0, replayed_count: 0, refused_count: 0, failed_count: 0,
    });

    await expect(sent).resolves.toMatchObject({ receipt_id: 'command-key-1' });
  });

  it('clears exactly the named bots with one command body under the frozen key', async () => {
    const sent = service.clearBots(target('account/1'), {
      idempotency_key: 'command-key-1',
      strategy_instance_ids: ['old-bot', 'older-bot'],
    });
    const request = http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/account%2F1/bots/clear');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toMatchObject({
      idempotency_key: 'command-key-1',
      strategy_instance_ids: ['old-bot', 'older-bot'],
      command_context: expect.objectContaining({
        capability: 'bot_action',
        idempotency_key: 'command-key-1',
        expected_effective_binding_generation: 3,
      }),
    });
    request.flush({
      account_id: 'account/1', receipt_id: 'command-key-1', recorded_at_ms: 2, legs: [],
      applied_count: 0, replayed_count: 0, refused_count: 0, failed_count: 0,
    });

    await expect(sent).resolves.toMatchObject({ receipt_id: 'command-key-1' });
  });

  it('refuses a cohort flatten whose body key disagrees with its frozen target', () => {
    expect(() =>
      service.runCohortFlatten(target('account/1'), {
        idempotency_key: 'a-different-key',
        legs: [],
      }),
    ).toThrow(/must match its frozen target/);
  });

  it('prepares and confirms one exact strategy/account Paper-access pairing', async () => {
    const prepared = service.preparePaperAccess(
      resourceTarget('alpaca paper', CLERK, {
        accountId: 'account/1',
        idempotencyKey: 'paper-access-key-1',
      }),
      'ema/crossover',
      'Review this exact strategy and account.',
    );
    const prepareRequest = http.expectOne(
      '/api/brokers/alpaca%20paper/clerks/clrk_spec/accounts/account%2F1/strategies/ema%2Fcrossover/paper-access/plan',
    );
    expect(prepareRequest.request.body).toEqual({
      reason: 'Review this exact strategy and account.',
    });
    prepareRequest.flush({
      schema_version: 1,
      plan_id: 'a'.repeat(64),
      confirmation_token: 'a'.repeat(64),
      program_key: 'ema/crossover',
      account_id: 'account/1',
      actor: 'operator',
      reason: 'Review this exact strategy and account.',
      created_at_ms: 1,
      expires_at_ms: 2,
      ledger_path: '/tmp/test-ledger.json',
      expected_ledger_head_hash: null,
      evidence: {
        validation_event_id: 'validation-1',
        validation_snapshot_sha256: 'b'.repeat(64),
        program_version: '1',
        golden_trace_root: 'c'.repeat(64),
        running_artifact_digest: 'd'.repeat(64),
        qualification_receipt_hash: 'e'.repeat(64),
        qualification_suite: 'sealed-program',
        qualified_at_ms: 1,
      },
    });
    const plan = await prepared;

    const confirmed = service.confirmPaperAccess(
      resourceTarget('alpaca paper', CLERK, {
        accountId: 'account/1',
        idempotencyKey: 'paper-access-key-1',
      }),
      'ema/crossover',
      plan,
    );
    const confirmRequest = http.expectOne(
      '/api/brokers/alpaca%20paper/clerks/clrk_spec/accounts/account%2F1/strategies/ema%2Fcrossover/paper-access/confirm',
    );
    expect(confirmRequest.request.body).toMatchObject({
      plan,
      confirmation_token: 'a'.repeat(64),
    });
    expect((confirmRequest.request.body as { command_context: { capability: string } }).command_context.capability).toBe('deploy');
    confirmRequest.flush({
      schema_version: 1,
      sequence: 1,
      action: 'activated',
      program_key: 'ema/crossover',
      account_id: 'account/1',
      actor: 'operator',
      reason: plan.reason,
      recorded_at_ms: 2,
      evidence: plan.evidence,
      previous_event_hash: null,
      event_hash: 'f'.repeat(64),
    });

    await expect(confirmed).resolves.toMatchObject({ action: 'activated' });
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

  // Reconcile now asks for no confirmation and travels on the quiesce
  // operation, so after a 409 it is re-offered only when it is still enabled
  // with a new token; the two tests below cover the post-409 outcomes where
  // it must bail instead (disabled, or unchanged token).
  const staleReconcile: PanelAction = {
    action_id: 'reconcile_now',
    revision: 1,
    concurrency_token: 'tok-stale',
    enabled: true,
    label: 'Reconcile now',
    explanation: '',
    blockers: [],
    confirmation: null,
  };

  // A recovery action can remain available with a new evidence token.
  const staleRecovery: PanelAction = {
    action_id: 'resolve_execution_coverage', revision: 1,
    concurrency_token: 'tok-stale', enabled: true,
    label: 'Resolve execution coverage', explanation: '', blockers: [], confirmation: null,
  };

  const staleSafeFlatten: PanelAction = {
    action_id: 'execute_safe_flatten',
    revision: 1,
    concurrency_token: 'tok-stale',
    enabled: true,
    label: 'Execute safe flatten',
    explanation: '',
    blockers: [],
    confirmation: {
      title: 'Flatten attributed exposure?',
      body: 'Attributed exposure: AAPL 10.',
      consequence: 'The Clerk will submit reduction-only orders for the exact attributed quantities.',
      confirm_label: 'Flatten now',
      required_token: '',
    },
  };

  const ACTIONS_URL = '/api/brokers/alpaca/clerks/clrk_spec/accounts/acct-1/bots/sid-1/actions';
  // The Clerk's stop, reconcile and safe flatten travel on their own
  // operation, which a draining lane still routes (#2351).
  const QUIESCE_URL = `${ACTIONS_URL}/quiesce`;
  const PANEL_URL = '/api/brokers/alpaca/clerks/clrk_spec/accounts/acct-1/bots/sid-1/panel';

  const conflict = () =>
    ({ detail: { message: 'stale' } });

  it('refetches a fresh token and retries once when a transient 409 clears (unconfirmed action)', async () => {
    const promise = service.runBotAction(target('acct-1', 'sid-1'), 'sid-1', staleRecovery);

    const first = http.expectOne(ACTIONS_URL);
    const firstIdempotencyKey = (first.request.body as {
      idempotency_key: string;
    }).idempotency_key;
    first.flush(conflict(), { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({
      actions: [{ ...staleRecovery, concurrency_token: 'tok-fresh' }],
    });
    await tick();

    const retry = http.expectOne(ACTIONS_URL);
    // The retry carries the CURRENT token, not the stale one.
    expect(retry.request.body.concurrency_token).toBe('tok-fresh');
    // It remains the same durable command despite its refreshed concurrency
    // token, so a transport retry cannot execute a second lifecycle action.
    expect(retry.request.body.idempotency_key).toBe(firstIdempotencyKey);
    retry.flush({
      action_id: 'resolve_execution_coverage',
      receipt_id: 'r-1',
      recorded_at_ms: 1,
      applied: true,
      revision: 2,
      concurrency_token: 'tok-fresh',
      message: 'Recovered',
    });

    await expect(promise).resolves.toMatchObject({ receipt_id: 'r-1' });
  });

  it('does NOT retry when the action is disabled after the 409 (state truly changed)', async () => {
    const promise = service.runBotAction(target('acct-1', 'sid-1'), 'sid-1', staleReconcile);

    http
      .expectOne(QUIESCE_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({
      actions: [{ ...staleReconcile, concurrency_token: 'tok-fresh', enabled: false }],
    });

    await expect(promise).rejects.toMatchObject({ status: 409 });
  });

  it('does NOT retry when the fresh token is unchanged (an availability 409)', async () => {
    const promise = service.runBotAction(target('acct-1', 'sid-1'), 'sid-1', staleReconcile);

    http
      .expectOne(QUIESCE_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });
    await tick();

    http.expectOne(PANEL_URL).flush({ actions: [staleReconcile] });

    await expect(promise).rejects.toMatchObject({ status: 409 });
  });

  it('does NOT retry an action that requires operator confirmation, even if its token changed and it is still enabled', async () => {
    // A safe flatten's token is bound to the reduction plan its confirmation
    // was shown for. A silent retry after a 409 could flatten a materially
    // different position than the one the operator confirmed — so confirmed
    // actions always re-throw and let the operator re-confirm explicitly.
    const promise = service.runBotAction(target('acct-1', 'sid-1'), 'sid-1', staleSafeFlatten);

    http
      .expectOne(QUIESCE_URL)
      .flush(conflict(), { status: 409, statusText: 'Conflict' });

    await expect(promise).rejects.toMatchObject({ status: 409 });
    http.expectNone(PANEL_URL);
  });

  it('re-throws a non-409 error without refetching the panel', async () => {
    const promise = service.runBotAction(target('acct-1', 'sid-1'), 'sid-1', staleReconcile);

    http
      .expectOne(QUIESCE_URL)
      .flush({ detail: { message: 'boom' } }, { status: 500, statusText: 'Server Error' });

    await expect(promise).rejects.toMatchObject({ status: 500 });
    http.expectNone(PANEL_URL);
  });

  it('sends a stop on the quiesce operation a draining lane routes, and recovery on /actions', async () => {
    const stop = service.runBotAction(target('acct-1', 'sid-1'), 'sid-1', fakeSqliteStopAction());
    const stopRequest = http.expectOne(QUIESCE_URL);
    expect(stopRequest.request.method).toBe('POST');
    expect(stopRequest.request.body.action_id).toBe('stop_bot_decisions');
    stopRequest.flush({ action_id: 'stop_bot_decisions', receipt_id: 'r-stop', recorded_at_ms: 1, applied: true });
    await expect(stop).resolves.toMatchObject({ receipt_id: 'r-stop' });

    const recovery = service.runBotAction(target('acct-1', 'sid-1'), 'sid-1', staleRecovery);
    http.expectNone(QUIESCE_URL);
    http
      .expectOne(ACTIONS_URL)
      .flush({ action_id: 'resolve_execution_coverage', receipt_id: 'r-stop', recorded_at_ms: 1, applied: true });
    await expect(recovery).resolves.toMatchObject({ receipt_id: 'r-stop' });
  });

  const request = (actionId: PanelActionRequest['action_id']): PanelActionRequest => ({
    action_id: actionId,
    revision: 1,
    concurrency_token: 'tok',
    idempotency_key: 'command-key-1',
    reason: null,
  });
  const done = (actionId: string) => ({
    action_id: actionId,
    receipt_id: `r-${actionId}`,
    recorded_at_ms: 1,
    applied: true,
  });

  it.each([
    'stop_bot_decisions',
    'cancel_verified_working_orders',
    'execute_safe_flatten',
    'reconcile_now',
  ] as const)('sends the quiesce action %s on the operation a draining lane routes', async (actionId) => {
    const pending = service.runAction(target('acct-1', 'sid-1'), 'sid-1', request(actionId));
    http.expectOne(QUIESCE_URL).flush(done(actionId));
    await expect(pending).resolves.toMatchObject({ action_id: actionId });
  });

  it.each(['resolve_execution_coverage', 'archive'] as const)(
    'sends %s on /actions, which a draining lane refuses',
    async (actionId) => {
      const pending = service.runAction(target('acct-1', 'sid-1'), 'sid-1', request(actionId));
      http.expectOne(ACTIONS_URL).flush(done(actionId));
      await expect(pending).resolves.toMatchObject({ action_id: actionId });
    },
  );

  it('falls back to /actions under a derived key when the quiesce route is not deployed yet', async () => {
    const pending = service.runAction(target('acct-1', 'sid-1'), 'sid-1', request('stop_bot_decisions'));
    http
      .expectOne(QUIESCE_URL)
      .flush({ detail: 'Not Found' }, { status: 404, statusText: 'Not Found' });
    await tick();

    const fallback = http.expectOne(ACTIONS_URL);
    expect(fallback.request.body.idempotency_key).toBe('command-key-1:actions');
    expect(fallback.request.body.command_context.idempotency_key).toBe('command-key-1:actions');
    fallback.flush(done('stop_bot_decisions'));
    await expect(pending).resolves.toMatchObject({ receipt_id: 'r-stop_bot_decisions' });
  });

  it.each([
    ['a typed fleet refusal', { reason: 'clerk_not_found', message: 'No clerk carries this identity.' }],
    ['a typed panel refusal', { detail: { message: 'Unknown bot.' } }],
  ])('never falls back on %s', async (_label, body) => {
    const pending = service.runAction(target('acct-1', 'sid-1'), 'sid-1', request('stop_bot_decisions'));
    http.expectOne(QUIESCE_URL).flush(body, { status: 404, statusText: 'Not Found' });

    await expect(pending).rejects.toMatchObject({ status: 404 });
    http.expectNone(ACTIONS_URL);
  });

  it('rejects a bot action whose interaction owner did not freeze a durable key', async () => {
    const unkeyed = resourceTarget('alpaca', CLERK, {
      accountId: 'acct-1',
      entityId: 'sid-1',
      bindingGeneration: 3,
      routingEpoch: 4,
    });

    await expect(service.runBotAction(unkeyed, 'sid-1', staleRecovery))
      .rejects.toThrow(/interaction owner/i);
    http.expectNone(ACTIONS_URL);
  });
});

describe('BrokerV2PanelService polled reads', () => {
  let service: BrokerV2PanelService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerV2PanelService);
    http = TestBed.inject(HttpTestingController);
  });

  it('rejects a catalog read that never responds, so the poll can recover', async () => {
    // S7: the roster poll skips every tick while the previous request is
    // still in flight. HttpClient never times out on its own, so one hung
    // request across a data-plane restart froze the roster for 9+ minutes
    // while the footer kept looking fresh. A finite timeout turns the hang
    // into a rejection the existing error affordance already handles.
    vi.useFakeTimers();
    try {
      const pending = service.getCatalog(resourceTarget('alpaca', CLERK, { accountId: 'acct-1' }));
      const rejection = expect(pending).rejects.toBeTruthy();
      http.expectOne('/api/brokers/alpaca/clerks/clrk_spec/accounts/acct-1/bots/catalog');

      await vi.advanceTimersByTimeAsync(POLL_REQUEST_TIMEOUT_MS + 1);

      await rejection;
    } finally {
      vi.useRealTimers();
    }
  });
});
