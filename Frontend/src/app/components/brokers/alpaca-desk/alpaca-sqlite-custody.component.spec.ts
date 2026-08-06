import { HttpErrorResponse } from '@angular/common/http';
import { By } from '@angular/platform-browser';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  SqliteClerkProjection,
  SqliteRecoveryAction,
  SqliteRecoveryResult,
  SqliteTimelinePage,
} from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { TypedHaltConfirmComponent } from '../../broker/bot-control/reused/typed-halt-confirm/typed-halt-confirm.component';
import { AlpacaSqliteCustodyComponent } from './alpaca-sqlite-custody.component';

const NOW = 1_700_000_000_000;

function action(
  overrides: Partial<SqliteRecoveryAction> = {},
): SqliteRecoveryAction {
  return {
    action_id: 'reconcile_now',
    label: 'Reconcile now',
    explanation: 'Compare durable Clerk custody with a fresh Alpaca observation.',
    available: true,
    unavailable_reason_code: null,
    unavailable_reason: null,
    scope: 'ACCOUNT_CLERK',
    freshness: 'not_required',
    evidence: [],
    confirmation: null,
    next_step: 'Run the account comparison now.',
    concurrency_token: 'token-17',
    execution_ref: null,
    mutation: true,
    primary: true,
    ...overrides,
  };
}

function projection(
  actions: readonly SqliteRecoveryAction[] = [action()],
): SqliteClerkProjection {
  return {
    account_id: 'PA1',
    strategy_instance_id: null,
    authority_generation: 4,
    db_identity_token: 'db-generation-4',
    authority_health: 'healthy',
    authority_health_reason: null,
    control_revision: 17,
    custody_owner: 'ACCOUNT_CLERK',
    runs: [],
    commands: [],
    operations: [],
    positions: [],
    holds: [],
    uncertainties: [],
    latest_reconciliation: null,
    terminal_receipts: [],
    guidance: {
      headline: 'Account Clerk custody is healthy',
      explanation: 'Durable Clerk state has no unresolved uncertainty.',
      scope: 'ACCOUNT_CLERK',
      impact: 'Normal Clerk-governed controls remain available.',
      custody_owner: 'ACCOUNT_CLERK',
      may_create_exposure: true,
      available_safety_actions: actions.filter((item) => item.available).map((item) => item.label),
      action_required: false,
      next_step: 'No recovery action is required.',
    },
    recovery_actions: [...actions],
    generated_at_ms: NOW,
  };
}

function timeline(): SqliteTimelinePage {
  return {
    account_id: 'PA1',
    strategy_instance_id: null,
    authority_generation: 4,
    control_revision: 17,
    anchor_sequence: 12,
    total_entries: 1,
    next_cursor: null,
    entries: [{
      sequence: 12,
      operation_ref: 'effect:enter:12',
      effect_operation_id: 'effect:enter:12',
      command_id: 'command:enter:12',
      order_ref: 'order:enter:12',
      broker_order_id: 'alpaca-order-12',
      transition_kind: 'ORDER_EVIDENCE_OBSERVED',
      operation_state: 'in_progress',
      broker_state: 'accepted',
      custody_owner: 'ACCOUNT_CLERK',
      execution_authority: 'ACCOUNT_CLERK',
      summary_code: 'ORDER_EVIDENCE_OBSERVED',
      proof_reference: 'proof:12',
      source_event_at_ms: NOW - 300,
      clerk_observed_at_ms: NOW - 200,
      recorded_at_ms: NOW - 100,
    }],
  };
}

async function renderCustody(service: Partial<BrokersService>) {
  return render(AlpacaSqliteCustodyComponent, {
    inputs: { accountId: 'PA1' },
    providers: [{ provide: BrokersService, useValue: service }],
  });
}

describe('AlpacaSqliteCustodyComponent', () => {
  it('renders nothing when the account still has legacy authority', async () => {
    const getSqliteClerkProjection = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 409 }),
    );

    await renderCustody({ getSqliteClerkProjection });

    await waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledOnce());
    expect(screen.queryByRole('heading', { name: 'Account Clerk' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders backend-authored guidance and all three custody clocks', async () => {
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([
        action({
          action_id: 'open_custody_timeline',
          label: 'Open custody timeline',
          explanation: 'Inspect immutable custody evidence.',
          mutation: false,
        }),
      ])),
      getSqliteClerkTimeline: vi.fn().mockResolvedValue(timeline()),
    });

    expect(await screen.findByText('Durable Clerk state has no unresolved uncertainty.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open custody timeline' }));

    expect(await screen.findByText('effect:enter:12')).toBeTruthy();
    expect(screen.getByText('Source event')).toBeTruthy();
    expect(screen.getByText('Clerk observed')).toBeTruthy();
    expect(screen.getByText('Durably recorded')).toBeTruthy();
  });

  it('labels an unverified activation identity without presenting generation zero', async () => {
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue({
        ...projection([]),
        authority_generation: 0,
        db_identity_token: 'unverified-activation',
        authority_health: 'failed',
      }),
    });

    expect(await screen.findByText(/activation identity unverified/i)).toBeTruthy();
    expect(screen.queryByText(/generation 0/i)).toBeNull();
  });

  it('confirms and executes the exact evidence-bound mutation', async () => {
    const cancel = action({
      action_id: 'cancel_verified_working_orders',
      label: 'Cancel verified working orders',
      explanation: 'Cancel only order:entry:12.',
      freshness: 'fresh',
      evidence: [{
        reference: 'order:order:entry:12',
        label: 'Verified working order',
        observed_at_ms: NOW - 100,
        age_ms: 100,
        freshness: 'fresh',
      }],
      confirmation: {
        title: 'Cancel verified orders?',
        explanation: 'Only the listed order will be canceled.',
        confirm_label: 'Cancel verified orders',
      },
    });
    const receipt = {
      action_id: cancel.action_id,
      outcome: 'success',
      applied: true,
      receipt_id: 'order:entry:12',
      recorded_at_ms: NOW + 20,
      command: null,
      reconciliation: null,
      orders: [],
    } satisfies SqliteRecoveryResult;
    const executeSqliteRecoveryAction = vi.fn().mockResolvedValue(receipt);
    const { fixture } = await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([cancel])),
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: cancel.label }));
    const confirmation = fixture.debugElement.query(By.directive(TypedHaltConfirmComponent));
    expect(confirmation).toBeTruthy();
    confirmation.componentInstance.confirmed.emit();

    await waitFor(() => {
      expect(executeSqliteRecoveryAction).toHaveBeenCalledWith('PA1', cancel);
    });
    expect(await screen.findByText('order:entry:12')).toBeTruthy();
  });

  it('never auto-retries a stale action and refreshes the projection', async () => {
    const getSqliteClerkProjection = vi.fn().mockResolvedValue(projection());
    const executeSqliteRecoveryAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 409 }),
    );
    await renderCustody({
      getSqliteClerkProjection,
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Reconcile now' }));

    expect(await screen.findByText(/Clerk evidence changed/)).toBeTruthy();
    expect(executeSqliteRecoveryAction).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledTimes(2));
  });

  it('renders policy-authored unavailability and no generic recovery action', async () => {
    const unavailable = action({
      action_id: 'cancel_verified_working_orders',
      label: 'Cancel verified working orders',
      available: false,
      unavailable_reason_code: 'NO_VERIFIED_WORKING_ORDERS',
      unavailable_reason: 'No exact working-order identity is proven.',
      primary: false,
    });
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([unavailable])),
    });

    const button = await screen.findByRole('button', { name: unavailable.label });
    expect(button.hasAttribute('disabled')).toBe(true);
    expect(screen.getByText('No exact working-order identity is proven.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /clear hold/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /flatten/i })).toBeNull();
  });
});
