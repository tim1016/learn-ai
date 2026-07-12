import { describe, expect, it } from 'vitest';

import { makeStatus } from '../bot-control-page.fixtures';
import { resolveTraderViewModel } from './trader-view.model';

describe('resolveTraderViewModel', () => {
  it('presents backend status as a truthful trader summary', () => {
    const status = makeStatus({ startCapabilityEnabled: true });
    status.operator_surface.trading_session = {
      phase: 'CLOSED',
      permits_strategy_activity: false,
      next_transition_ms: Date.UTC(2026, 6, 13, 13, 30),
      timezone: 'America/Chicago',
      as_of_ms: Date.UTC(2026, 6, 11, 20),
    };
    status.operator_surface.current_risk = {
      posture: 'FLAT',
      owned_positions: {},
      pending_order_count: 0,
      verdict: 'READY',
      unrealized_pnl: 0,
    };
    status.operator_surface.broker.connection = 'CONNECTED';
    status.operator_surface.reconciliation = {
      state: 'CLEAN',
      failure_reason: null,
      adopted_intent_ids: [],
      last_reconcile_ms: 1,
      sidecar_wal_seq: 1,
      broker_observed_at_ms: 1,
    };

    const model = resolveTraderViewModel(status, null);

    expect(model.headline).toBe('This bot is ready');
    expect(model.marketTitle).toBe('Market closed');
    expect(model.marketDetail).toContain('Market opens Mon');
    expect(model.metrics.map((metric) => metric.value)).toEqual(['$0.00', 'Flat', '0', '0']);
    expect(model.trustRows.map((row) => row.value)).toContain('Account checked');
    expect(model.primaryActionLabel).toBe('Start');
  });

  it('does not turn missing broker facts into healthy zeroes', () => {
    const model = resolveTraderViewModel(makeStatus(), null);

    expect(model.metrics.map((metric) => metric.value)).toEqual([
      'Not proven',
      'Not proven',
      'Not proven',
      'Not proven',
    ]);
    expect(model.trustRows.find((row) => row.label === 'Account safety')?.value)
      .toBe('Not proven');
  });
});
