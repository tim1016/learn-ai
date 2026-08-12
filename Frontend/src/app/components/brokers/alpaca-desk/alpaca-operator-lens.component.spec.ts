import { signal } from '@angular/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ClerkStatus, SqliteClerkProjection } from '../../../api/alpaca.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';
import { AlpacaOperatorLensComponent } from './alpaca-operator-lens.component';

function clerkStatus(): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    hold: { active: false, reason_code: null, reason: null, since_ms: null },
    latest_reconciliation: null,
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
    authority_kind: 'sqlite',
    channel_healths: [],
    generic_hold_clear_available: false,
    generic_hold_clear_explanation: null,
  };
}

function projection(): SqliteClerkProjection {
  return {
    account_id: 'PA1', strategy_instance_id: null, authority_generation: 1,
    db_identity_token: 'db-1', authority_health: 'healthy', authority_health_reason: null,
    control_revision: 3, custody_owner: 'ACCOUNT_CLERK', runs: [], commands: [], operations: [],
    positions: [], holds: [], uncertainties: [], latest_reconciliation: null, terminal_receipts: [],
    guidance: {
      headline: 'Account Clerk custody is healthy',
      explanation: 'Durable Clerk state has no unresolved uncertainty.', scope: 'ACCOUNT_CLERK',
      impact: 'Normal Clerk-governed controls remain available.', custody_owner: 'ACCOUNT_CLERK',
      may_create_exposure: true, available_safety_actions: [], action_required: false,
      next_step: 'No recovery action is required.',
    },
    recovery_actions: [], generated_at_ms: 1_700_000_000_000,
  };
}

function resourceValue<T>(value: T) {
  return {
    isLoading: signal(false),
    error: signal<unknown>(undefined),
    value: signal<T | undefined>(value),
  };
}

describe('AlpacaOperatorLensComponent', () => {
  it('forwards the inline repair to the existing evidence-bound custody action flow', async () => {
    const repair = {
      action_id: 'reconcile_now' as const,
      label: 'Reconcile now',
      explanation: 'Compare Clerk custody to a fresh broker observation.',
      available: true,
      unavailable_reason_code: null,
      unavailable_reason: null,
      scope: 'ACCOUNT_CLERK' as const,
      freshness: 'fresh' as const,
      evidence: [],
      reduction_plan: null,
      confirmation: null,
      next_step: 'Run reconciliation now.',
      concurrency_token: 'token-1',
      execution_ref: null,
      mutation: true,
      primary: true,
    };
    const activeProjection = {
      ...projection(),
      guidance: {
        ...projection().guidance,
        headline: 'Clerk reconciliation required',
        action_required: true,
      },
      recovery_actions: [repair],
    };
    const executeSqliteRecoveryAction = vi.fn().mockResolvedValue({
      applied: true,
      receipt_id: 'receipt-1',
      recorded_at_ms: 1_700_000_000_001,
    });

    await render(AlpacaOperatorLensComponent, {
      providers: [
        {
          provide: AlpacaOperatorLensDataService,
          useValue: { status: resourceValue(clerkStatus()), projection: resourceValue(activeProjection) },
        },
        { provide: BrokerService, useValue: { accountTransactions: vi.fn(), accountTransaction: vi.fn() } },
        {
          provide: BrokersService,
          useValue: {
            getSqliteClerkProjection: vi.fn().mockResolvedValue(activeProjection),
            executeSqliteRecoveryAction,
          },
        },
      ],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile now' }));

    await waitFor(() => {
      expect(executeSqliteRecoveryAction).toHaveBeenCalledWith('PA1', repair);
    });
  });

  it('composes the canonical forensic grid, filters, and shared receipt reader', async () => {
    const status = resourceValue(clerkStatus());
    const sqliteProjection = resourceValue(projection());
    const accountTransaction = vi.fn().mockResolvedValue({
      transaction_id: 'txn-1', broker: 'alpaca', account_id: 'PA1', journal_seq: 1,
      recorded_at_ms: 1_700_000_000_000, transaction_kind: 'strategy_execution',
      transaction_origin: 'strategy', strategy_instance_id: 'bot-1', run_id: 'run-1',
      intent_id: 'intent-1', order_ref: 'order-1', order_id: null, perm_id: null,
      exec_id: null, native_order_id: null, native_execution_id: null,
      lifecycle_state: 'filled', commission_status: 'reported', fee: 0.1,
      receipt: {}, events: [], custody_timeline: null,
    });
    const accountTransactions = vi.fn().mockResolvedValue({
      projection_available: true, canonical_fallback_required: false, feed_state: 'live',
      feed_headline: 'Current', feed_detail: 'Projection current', high_water_journal_seq: 1,
      lag_records: 0, lag_is_lower_bound: false,
      custody_summary: { record_count: 1, a0_custody_accepted_count: 1, a1_broker_write_started_count: 1, a2_broker_known_count: 1, a3_economic_terminal_count: 1, uncertain_count: 0 },
      rows: [{
        transaction_id: 'txn-1', broker: 'alpaca', account_id: 'PA1', journal_seq: 1,
        recorded_at_ms: 1_700_000_000_000, transaction_kind: 'strategy_execution',
        transaction_origin: 'strategy', strategy_instance_id: 'bot-1', run_id: 'run-1',
        intent_id: 'intent-1', order_ref: 'order-1', order_id: null, perm_id: null,
        exec_id: null, native_order_id: null, native_execution_id: null,
        lifecycle_state: 'filled', commission_status: 'reported', fee: 0.1,
        event_count: 1,
      }], next_cursor: null,
    });
    await render(AlpacaOperatorLensComponent, {
      providers: [
        { provide: AlpacaOperatorLensDataService, useValue: { status, projection: sqliteProjection } },
        {
          provide: BrokerService,
          useValue: {
            accountTransactions,
            accountTransaction,
          },
        },
        { provide: BrokersService, useValue: {} },
      ],
    });

    expect(screen.getByRole('heading', { name: 'System healthy' })).toBeTruthy();
    await waitFor(() => expect(accountTransactions).toHaveBeenCalledWith('PA1', null, 25, {}));
    expect(screen.getByText('Recorded')).toBeTruthy();
    expect(screen.getByText('Origin and context')).toBeTruthy();
    expect(screen.getByText('Instruction and execution')).toBeTruthy();
    expect(screen.getAllByText('Lifecycle')).toHaveLength(2);
    expect(screen.getByText('Evidence')).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Transaction history filters' })).toBeTruthy();
    expect(screen.getByRole('combobox', { name: 'Origin' })).toBeTruthy();
    expect(screen.getByRole('textbox', { name: 'Lifecycle' })).toBeTruthy();
    expect(screen.getByRole('textbox', { name: 'Bot' })).toBeTruthy();
    expect(screen.getByRole('textbox', { name: 'Run' })).toBeTruthy();
    expect(document.querySelector('app-clerk-transaction-evidence-drawer')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Open receipt order-1' }));
    await waitFor(() => expect(accountTransaction).toHaveBeenCalledWith('PA1', 'txn-1'));
  });

  it('keeps all deep system panels collapsed until the operator opens one', async () => {
    const status = resourceValue(clerkStatus());
    const sqliteProjection = resourceValue(projection());
    const { container } = await render(AlpacaOperatorLensComponent, {
      providers: [
        { provide: AlpacaOperatorLensDataService, useValue: { status, projection: sqliteProjection } },
        { provide: BrokerService, useValue: { accountTransactions: vi.fn(), accountTransaction: vi.fn() } },
        { provide: BrokersService, useValue: {} },
      ],
    });

    const panels = Array.from(container.querySelectorAll<HTMLDetailsElement>('.operator-lens__deep details'));
    expect(panels).toHaveLength(4);
    expect(panels.every((panel) => !panel.open)).toBe(true);

    fireEvent.click(screen.getByText('Truth spine'));
    expect(panels[3].open).toBe(true);
    expect(screen.getAllByText('Account service')).not.toHaveLength(0);
    expect(screen.getByText('Healthy')).toBeTruthy();
    expect(screen.queryByText('ACCOUNT_CLERK')).toBeNull();
    expect(screen.queryByText('healthy')).toBeNull();

    fireEvent.click(screen.getByText('Broker session and capability'));
    expect(screen.queryByText('1700000000000')).toBeNull();
  });
});
