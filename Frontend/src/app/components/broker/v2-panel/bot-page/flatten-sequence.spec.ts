import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';
import type {
  SqliteExtendedLimitPricing,
  SqliteRecoveryAction,
  SqliteRecoveryActionCheck,
  SqliteSafeFlattenPlan,
} from '../../../../api/alpaca.types';
import { resourceTarget, withCommand } from '../../../../fleet/resource-target';
import { fakeBotPanelView, fakePanelAction } from '../../../../testing/bot-panel-fixtures';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { PanelActionResult } from '../lib/broker-v2-panel.types';
import {
  flattenUnderway,
  initialFlattenSteps,
  refreshedLimitTicket,
  runFlattenSequence,
  settleFlattenStep,
  type FlattenSequenceDeps,
} from './flatten-sequence';

const PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token', account_id: 'PA9', authority_generation: 4, db_identity_token: 'db-4',
  control_revision: 17, scope: 'CUSTODY_SUBJECT', strategy_instance_id: 'spy-a', reconciliation_id: 'rec-17',
  account_check_kind: 'reconcile_now', account_check_observed_at_ms: 1_753_799_999_500,
  prepared_at_ms: 1_753_800_000_000, expires_at_ms: 4_102_444_800_000,
  legs: [{ strategy_instance_id: 'spy-a', symbol: 'SPY', side: 'sell', quantity: 3, position_updated_at_ms: 1_753_799_999_000 }],
};

const CAPABILITY: SqliteRecoveryAction = {
  action_id: 'prepare_safe_flatten', label: 'Prepare safe flatten', explanation: 'Prepare a fresh reduction plan.',
  available: true, unavailable_reason_code: null, unavailable_reason: null, scope: 'CUSTODY_SUBJECT',
  freshness: 'fresh', evidence: [], reduction_plan: PLAN, confirmation: null,
  next_step: 'Review the plan.', concurrency_token: 'plan-token', execution_ref: null, mutation: false, primary: false,
};

const EXTENDED: SqliteExtendedLimitPricing = {
  kind: 'extended_limit', phase: 'POST', symbol: 'SPY', side: 'sell',
  bid: 500.1, ask: 500.2, bid_size: 100, ask_size: 100, quote_observed_at_ms: 1_753_800_000_000,
  quote_max_age_ms: 10_000, exit_allowance_bps: 20, suggested_limit_price: 499.1, band_limit_price: 498.1,
  spread: 0.1, spread_bps: 2, wide_spread: false, spread_warning_bps: 50, proposal: null,
};

const NEXT_OPEN_MS = 1_753_862_400_000;

function check(pricing: SqliteRecoveryActionCheck['reduction_pricing'], capability = CAPABILITY): SqliteRecoveryActionCheck {
  return { capability, reduction_pricing: pricing };
}

function result(message: string): PanelActionResult {
  return {
    action_id: 'reconcile_now', outcome: 'success', receipt_id: 'receipt-1', recorded_at_ms: 1_753_800_000_000,
    applied: true, revision: 18, concurrency_token: 'next', message,
  };
}

/** A stopped bot's panel that offers Reconcile, then Prepare, then Execute — as the Clerk mints them. */
function deps(planCheck: SqliteRecoveryActionCheck, overrides: Partial<FlattenSequenceDeps> = {}) {
  const panel = fakeBotPanelView({
    actions: [
      fakePanelAction('reconcile_now'),
      fakePanelAction('prepare_safe_flatten'),
      fakePanelAction('execute_safe_flatten'),
    ],
  });
  const reports: [string, string, string | null][] = [];
  const bound: FlattenSequenceDeps = {
    readPanel: vi.fn().mockResolvedValue(panel),
    runAction: vi.fn().mockResolvedValue(result('Reconciled.')),
    checkPlan: vi.fn().mockResolvedValue(planCheck),
    report: (step, state, message) => reports.push([step, state, message]),
    ...overrides,
  };
  return { bound, reports };
}

describe('the flatten sequence', () => {
  it.each([
    ['SIMULATED_RECOVERY_PRICE_UNAVAILABLE', null],
    ['NO_SESSION_OPEN', NEXT_OPEN_MS],
  ] as const)('fails the Sell step on a refused pricing (%s) and carries its code and next open', async (code, opensAt) => {
    const { bound, reports } = deps(check({
      kind: 'refused', reason_code: code, explanation: 'No price can be set now.',
      next_step: 'Flatten again later.', available_at_ms: opensAt,
    }));

    const outcome = await runFlattenSequence(bound, { symbol: 'SPY', quantity: 3 });

    expect(outcome).toEqual({
      kind: 'failed', step: 'sell',
      rejection: {
        outcome: 'failure', message: 'No price can be set now.', why: 'Flatten again later.',
        reasonCode: code, availableAtMs: opensAt, nextAction: null,
      },
    });
    expect(reports.at(-1)).toEqual(['sell', 'failed', 'No price can be set now.']);
    expect(bound.runAction).toHaveBeenCalledTimes(1);
  });

  it('fails the Sell step when the Clerk does not say how the sale would go out', async () => {
    const { bound, reports } = deps(check(null));

    const outcome = await runFlattenSequence(bound, { symbol: 'SPY', quantity: 3 });

    expect(outcome.kind === 'failed' && [outcome.step, outcome.rejection.message]).toEqual([
      'sell', 'The prepared sale did not say how it would be sent. Nothing was sent.',
    ]);
    expect(reports.at(-1)?.slice(0, 2)).toEqual(['sell', 'failed']);
    expect(bound.runAction).toHaveBeenCalledTimes(1);
  });

  it('stops at the check step when Reconcile is refused, with the backend’s code', async () => {
    const { bound, reports } = deps(check({ kind: 'regular_session' }), {
      runAction: vi.fn().mockRejectedValue(new HttpErrorResponse({
        status: 409,
        error: { detail: { reason: 'stale_action_token', message: 'The custody state changed.' } },
      })),
    });

    const outcome = await runFlattenSequence(bound, { symbol: 'SPY', quantity: 3 });

    expect(outcome.kind === 'failed' && [outcome.step, outcome.rejection.message, outcome.rejection.reasonCode])
      .toEqual(['reconcile', 'The custody state changed.', 'stale_action_token']);
    expect(reports).toEqual([['reconcile', 'running', null], ['reconcile', 'failed', 'The custody state changed.']]);
    expect(bound.checkPlan).not.toHaveBeenCalled();
  });

  it('stops at the plan step when the Clerk has no plan, with its reason code', async () => {
    const { bound } = deps(check({ kind: 'regular_session' }, {
      ...CAPABILITY, reduction_plan: null, unavailable_reason_code: 'POSITION_UNPROVEN',
      unavailable_reason: 'The position is not proven.',
    }));

    const outcome = await runFlattenSequence(bound, { symbol: 'SPY', quantity: 3 });

    expect(outcome.kind === 'failed' && [outcome.step, outcome.rejection.reasonCode, outcome.rejection.why])
      .toEqual(['plan', 'POSITION_UNPROVEN', 'Review the plan.']);
  });

  it('leaves the Sell step running on the limit ticket outside regular hours', async () => {
    const { bound, reports } = deps(check(EXTENDED));

    const outcome = await runFlattenSequence(bound, { symbol: 'SPY', quantity: 3 });

    expect(outcome).toEqual({ kind: 'needs_limit', plan: PLAN, pricing: EXTENDED });
    expect(reports.at(-1)).toEqual([
      'sell', 'running', 'Outside regular hours this sale needs a limit price. Set it below.',
    ]);
  });

  it('stops on the lane’s own refusal of Reconcile, after one request under its key (#2874)', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    const service = TestBed.inject(BrokerV2PanelService);
    const http = TestBed.inject(HttpTestingController);
    const lane = resourceTarget('alpaca', 'clrk_spec', {
      accountId: 'PA9', entityId: 'spy-a', bindingGeneration: 3, routingEpoch: 4,
    });
    const bot = '/api/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/spy-a';
    const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
    const reports: [string, string, string | null][] = [];
    const checkPlan = vi.fn();

    // The page's own binding: the panel read, and each command under a key of its own.
    const outcome = runFlattenSequence({
      readPanel: () => service.getPanel(lane, 'spy-a'),
      runAction: (action) => service.runBotAction(withCommand(lane, 'bot_action', 'reconcile-key-1'), 'spy-a', action),
      checkPlan,
      report: (step, state, message) => reports.push([step, state, message]),
    }, { symbol: 'SPY', quantity: 3 });
    http.expectOne(`${bot}/panel`).flush(fakeBotPanelView({ actions: [fakePanelAction('reconcile_now')] }));
    await tick();
    http.expectOne(`${bot}/actions/quiesce`).flush(
      { detail: { outcome: 'conflict', message: 'The custody state changed.', why: 'Refresh, then flatten again.' } },
      { status: 409, statusText: 'Conflict' },
    );
    await tick();

    // Nothing follows the refusal: no second Reconcile under the spent key,
    // which the coordinator would answer in place of the lane.
    http.verify();
    await expect(outcome).resolves.toMatchObject({
      kind: 'failed', step: 'reconcile',
      rejection: { outcome: 'conflict', message: 'The custody state changed.', why: 'Refresh, then flatten again.' },
    });
    expect(reports).toEqual([['reconcile', 'running', null], ['reconcile', 'failed', 'The custody state changed.']]);
    expect(checkPlan).not.toHaveBeenCalled();
  });
});

describe('the flatten steps', () => {

  it('changes one step at a time and knows while one is still running', () => {
    const running = settleFlattenStep(initialFlattenSteps('trade'), 'sell', 'running', 'Set it below.');

    expect(flattenUnderway(running)).toBe(true);
    const settled = settleFlattenStep(running, 'sell', 'done', 'Limit order sent.');
    expect(settled?.map((step) => [step.id, step.state, step.message])).toEqual([
      ['reconcile', 'waiting', null], ['plan', 'waiting', null], ['sell', 'done', 'Limit order sent.'],
    ]);
    expect(flattenUnderway(settled)).toBe(false);
    expect(settleFlattenStep(null, 'sell', 'done', null)).toBeNull();
  });
});

describe('a refreshed limit ticket', () => {
  it('stays open while the plan still goes out as a limit order', () => {
    expect(refreshedLimitTicket(check(EXTENDED))).toEqual({ kind: 'ticket', plan: PLAN, pricing: EXTENDED });
  });

  it('ends with the Clerk’s reason when the plan is gone', () => {
    const refreshed = refreshedLimitTicket(check(EXTENDED, {
      ...CAPABILITY, reduction_plan: null, unavailable_reason_code: 'FLAT', unavailable_reason: 'The bot is flat.',
    }));

    expect(refreshed).toEqual({
      kind: 'ended',
      rejection: { outcome: 'failure', message: 'The bot is flat.', why: 'Review the plan.', reasonCode: 'FLAT', availableAtMs: null, nextAction: null },
    });
  });

  it('ends with the next session’s open when the session closed', () => {
    const refreshed = refreshedLimitTicket(check({
      kind: 'refused', reason_code: 'NO_SESSION_OPEN', explanation: 'No session is open.',
      next_step: 'Flatten again once it opens.', available_at_ms: NEXT_OPEN_MS,
    }));

    expect(refreshed.kind === 'ended' && [refreshed.rejection.reasonCode, refreshed.rejection.availableAtMs])
      .toEqual(['NO_SESSION_OPEN', NEXT_OPEN_MS]);
  });

  it('ends when the regular session opened and the sale no longer takes a limit', () => {
    const refreshed = refreshedLimitTicket(check({ kind: 'regular_session' }));

    expect(refreshed.kind === 'ended' && [refreshed.rejection.message, refreshed.rejection.why]).toEqual([
      'The regular session has opened, so this sale no longer takes a limit price. Nothing was sent.',
      'Flatten again to sell at market.',
    ]);
  });
});
