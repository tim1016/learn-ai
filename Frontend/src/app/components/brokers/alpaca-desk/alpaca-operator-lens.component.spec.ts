import { signal } from '@angular/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ClerkStatus, SqliteClerkProjection } from '../../../api/alpaca.types';
import { ACCOUNT_DESK_CLERK_RECOVERY_ANCHOR, type AccountOperatorPosture } from '../../../api/operator-blocker.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokersService } from '../../../services/brokers.service';
import { healthyAccountOperatorPostureFixture, operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';
import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';
import { AlpacaOperatorLensComponent } from './alpaca-operator-lens.component';

function clerkStatus(posture: AccountOperatorPosture = healthyAccountOperatorPostureFixture()): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    hold: { active: false, reason_code: null, reason: null, since_ms: null },
    latest_reconciliation: null,
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
    authority_kind: 'sqlite',
    channel_healths: [],
    operator_posture: posture,
  };
}

/** The backend-authored posture for a fix_here custody condition (#1664). */
function fixHereAccountDeskPosture(): AccountOperatorPosture {
  const blocker = operatorBlockerFixture({
    host: 'account_desk',
    disposition: 'fix_here',
    headline: 'Clerk reconciliation required',
    detail: 'The Clerk needs a fresh broker observation.',
    primaryMove: {
      label: 'Open Clerk recovery',
      action: { kind: 'confirm_in_form', anchor: ACCOUNT_DESK_CLERK_RECOVERY_ANCHOR },
      target: null,
    },
  });
  return {
    condition: blocker.condition,
    account_desk: blocker,
    fleet_roster: { ...blocker, host: 'fleet_roster', disposition: 'fix_elsewhere' },
    status_headline: blocker.headline,
    status_detail: blocker.detail ?? null,
  };
}

/** A terminal authority-failure posture whose only move is open_runbook — no in-lens target exists yet. */
function terminalRunbookPosture(): AccountOperatorPosture {
  const blocker = operatorBlockerFixture({
    host: 'account_desk',
    disposition: 'terminal',
    headline: 'Alpaca execution is paused',
    detail: 'The Account Clerk authority is unavailable and cannot prove safe execution.',
    primaryMove: {
      label: 'Open Clerk recovery runbook',
      action: { kind: 'open_runbook', slug: 'alpaca-account-clerk-authority-recovery' },
      target: null,
    },
  });
  return {
    condition: blocker.condition,
    account_desk: blocker,
    fleet_roster: { ...blocker, host: 'fleet_roster' },
    status_headline: blocker.headline,
    status_detail: blocker.detail ?? null,
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

function getPortfolioHistory() {
  return vi.fn().mockResolvedValue({
    timestamps: [1_700_000_000_000, 1_700_086_400_000],
    equity: [100, 101],
    profit_loss: [0, 1],
    base_value: 100,
    timeframe: '1D',
  });
}

function lensDataProvider(
  currentProjection: SqliteClerkProjection = projection(),
  refreshProjection = vi.fn(),
  clerkStatusValue: ClerkStatus = clerkStatus(),
) {
  return {
    provide: AlpacaOperatorLensDataService,
    useValue: {
      status: resourceValue(clerkStatusValue),
      projection: resourceValue(currentProjection),
      projectionRefreshVersion: signal(0),
      refreshProjection,
    },
  };
}

describe('AlpacaOperatorLensComponent', () => {
  it('opens the recovery panel from the backend-authored fix_here move, then runs the presented action', async () => {
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
    const refreshProjection = vi.fn();

    await render(AlpacaOperatorLensComponent, {
      providers: [
        lensDataProvider(activeProjection, refreshProjection, clerkStatus(fixHereAccountDeskPosture())),
        { provide: BrokerService, useValue: { accountTransactions: vi.fn(), accountTransaction: vi.fn() } },
        {
          provide: BrokersService,
          useValue: {
            getSqliteClerkProjection: vi.fn().mockResolvedValue(activeProjection),
            executeSqliteRecoveryAction,
            getPortfolioHistory: getPortfolioHistory(),
          },
        },
      ],
    });

    expect(screen.getByRole('heading', { name: 'Clerk reconciliation required' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open Clerk recovery' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Reconcile now' })).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile now' }));

    await waitFor(() => {
      expect(executeSqliteRecoveryAction).toHaveBeenCalledWith('PA1', repair);
    });
    expect(refreshProjection).toHaveBeenCalledOnce();
  });

  it('does not render a dead button for a terminal open_runbook move it cannot dispatch', async () => {
    await render(AlpacaOperatorLensComponent, {
      providers: [
        lensDataProvider(projection(), vi.fn(), clerkStatus(terminalRunbookPosture())),
        { provide: BrokerService, useValue: { accountTransactions: vi.fn(), accountTransaction: vi.fn() } },
        { provide: BrokersService, useValue: { getPortfolioHistory: getPortfolioHistory() } },
      ],
    });

    expect(screen.getByRole('heading', { name: 'Alpaca execution is paused' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Open Clerk recovery runbook' })).toBeNull();
    expect(screen.getByText('This requires attention outside this desk.')).toBeTruthy();
    // The recovery panel must not have opened as a side effect of a click
    // that never had anywhere to dispatch a move.
    const custodyPanel = document.querySelector('.operator-lens__deep details');
    expect(custodyPanel?.hasAttribute('open')).toBe(false);
  });

  it('composes the canonical forensic grid, filters, and shared receipt reader', async () => {
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
        lensDataProvider(),
        {
          provide: BrokerService,
          useValue: {
            accountTransactions,
            accountTransaction,
          },
        },
        { provide: BrokersService, useValue: { getPortfolioHistory: getPortfolioHistory() } },
      ],
    });

    expect(screen.getByRole('heading', { name: 'Account Clerk custody is healthy' })).toBeTruthy();
    await waitFor(() => expect(accountTransactions).toHaveBeenCalledWith('PA1', null, 100, {
      fromMs: 1_700_000_000_000,
      toMs: 1_700_086_400_000,
    }));
    expect(screen.getByText('Recorded (local time)')).toBeTruthy();
    expect(screen.getByText('Instrument')).toBeTruthy();
    expect(screen.getByText('Request')).toBeTruthy();
    expect(screen.getByText('Execution')).toBeTruthy();
    expect(screen.getByText('Status')).toBeTruthy();
    expect(screen.getByText('Submitted by')).toBeTruthy();
    expect(screen.getByText('Fees')).toBeTruthy();
    expect(screen.getByText('Evidence')).toBeTruthy();
    expect(screen.getByPlaceholderText(/Search symbols, status, strategy/)).toBeTruthy();
    expect(document.querySelector('app-clerk-transaction-evidence-drawer')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'View evidence for order-1' }));
    await waitFor(() => expect(accountTransaction).toHaveBeenCalledWith('PA1', 'txn-1'));
  });

  it('keeps all deep system panels collapsed until the operator opens one', async () => {
    const { container } = await render(AlpacaOperatorLensComponent, {
      providers: [
        lensDataProvider(),
        { provide: BrokerService, useValue: { accountTransactions: vi.fn(), accountTransaction: vi.fn() } },
        { provide: BrokersService, useValue: { getPortfolioHistory: getPortfolioHistory() } },
      ],
    });

    const panels = Array.from(container.querySelectorAll<HTMLDetailsElement>('.operator-lens__deep details'));
    expect(panels).toHaveLength(4);
    expect(panels.every((panel) => !panel.open)).toBe(true);

    fireEvent.click(screen.getByText('Account source of truth'));
    expect(panels[3].open).toBe(true);
    expect(screen.getAllByText('Account service')).not.toHaveLength(0);
    expect(screen.getByText('Healthy')).toBeTruthy();
    expect(screen.queryByText('ACCOUNT_CLERK')).toBeNull();
    expect(screen.queryByText('healthy')).toBeNull();

    fireEvent.click(screen.getByText('Broker connection'));
    expect(screen.queryByText('1700000000000')).toBeNull();
  });
});
