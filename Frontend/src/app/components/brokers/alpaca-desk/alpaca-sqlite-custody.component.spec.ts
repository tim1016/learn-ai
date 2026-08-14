import { HttpErrorResponse } from '@angular/common/http';
import { By } from '@angular/platform-browser';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  SqliteClerkProjection,
  SqliteRecoveryAction,
  SqliteRecoveryResult,
  SqliteSafeFlattenPlan,
  SqliteTimelinePage,
} from '../../../api/alpaca.types';
import {
  BrokersService,
  type SqliteTimelineQuery,
} from '../../../services/brokers.service';
import { TypedHaltConfirmComponent } from '../../broker/shared/typed-halt-confirm/typed-halt-confirm.component';
import { AlpacaSqliteCustodyComponent } from './alpaca-sqlite-custody.component';

const NOW = 1_700_000_000_000;

const SAFE_FLATTEN_PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token-17',
  account_id: 'PA1',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  scope: 'ACCOUNT_CLERK',
  strategy_instance_id: null,
  reconciliation_id: 'reconciliation-17',
  prepared_at_ms: NOW,
  expires_at_ms: 4_102_444_800_000,
  legs: [{
    strategy_instance_id: 'spy-bot',
    symbol: 'SPY',
    side: 'sell',
    quantity: 1.25,
    position_updated_at_ms: NOW - 100,
  }],
};

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
    reduction_plan: null,
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

function timelineEntry(sequence: number): SqliteTimelinePage['entries'][number] {
  return {
    sequence,
    operation_ref: `effect:enter:${sequence}`,
    effect_operation_id: `effect:enter:${sequence}`,
    command_id: `command:enter:${sequence}`,
    order_ref: `order:enter:${sequence}`,
    broker_order_id: `alpaca-order-${sequence}`,
    transition_kind: 'ORDER_EVIDENCE_OBSERVED',
    operation_state: 'in_progress',
    broker_state: 'accepted',
    custody_owner: 'ACCOUNT_CLERK',
    execution_authority: 'ACCOUNT_CLERK',
    summary_code: 'ORDER_EVIDENCE_OBSERVED',
    proof_reference: `proof:${sequence}`,
    source_event_at_ms: NOW - 300,
    clerk_observed_at_ms: NOW - 200,
    recorded_at_ms: NOW - 100,
  };
}

function timeline(
  overrides: Partial<SqliteTimelinePage> = {},
): SqliteTimelinePage {
  return {
    account_id: 'PA1',
    strategy_instance_id: null,
    authority_generation: 4,
    control_revision: 17,
    anchor_sequence: 12,
    total_entries: 1,
    next_cursor: null,
    entries: [timelineEntry(12)],
    ...overrides,
  };
}

async function renderCustody(
  service: Partial<BrokersService>,
  inputs: { readonly timelineQuery?: SqliteTimelineQuery | null } = {},
) {
  return render(AlpacaSqliteCustodyComponent, {
    inputs: { accountId: 'PA1', ...inputs },
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
    expect(screen.queryByRole('heading', { name: 'Custody and recovery' })).toBeNull();
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

    expect((await screen.findAllByText('effect:enter:12')).length).toBeGreaterThan(0);
    expect(screen.getByText('Source event')).toBeTruthy();
    expect(screen.getByText('Clerk observed')).toBeTruthy();
    expect(screen.getByText('Durably recorded')).toBeTruthy();
  });

  it('paginates the custody timeline instead of silently truncating past the first page', async () => {
    const getSqliteClerkTimeline = vi.fn()
      .mockResolvedValueOnce(timeline({
        entries: [timelineEntry(12)],
        next_cursor: 'cursor-11',
        total_entries: 2,
      }))
      .mockResolvedValueOnce(timeline({
        entries: [timelineEntry(11)],
        next_cursor: null,
        total_entries: 2,
      }));
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([
        action({
          action_id: 'open_custody_timeline',
          label: 'Open custody timeline',
          explanation: 'Inspect immutable custody evidence.',
          mutation: false,
        }),
      ])),
      getSqliteClerkTimeline,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Open custody timeline' }));
    expect((await screen.findAllByText('effect:enter:12')).length).toBeGreaterThan(0);
    expect(screen.getByText('1 of 2')).toBeTruthy();

    const loadMore = screen.getByRole('button', { name: 'Load more events' });
    fireEvent.click(loadMore);

    expect((await screen.findAllByText('effect:enter:11')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('effect:enter:12').length).toBeGreaterThan(0);
    expect(screen.getByText('2 of 2')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Load more events' })).toBeNull();
    expect(getSqliteClerkTimeline).toHaveBeenNthCalledWith(2, 'PA1', { cursor: 'cursor-11' });
  });

  it('opens exact deep-link filters and updates immutable evidence on row selection', async () => {
    const getSqliteClerkTimeline = vi.fn().mockResolvedValue(timeline({
      entries: [timelineEntry(12), timelineEntry(11)],
      total_entries: 2,
    }));
    const query: SqliteTimelineQuery = {
      strategyInstanceId: 'spy-bot',
      orderRef: 'order:enter:12',
      uncertaintyId: 'uncertainty:12',
      executionId: 'execution:12',
      transitionKind: 'ORDER_EVIDENCE_OBSERVED',
      sequence: 12,
    };
    await renderCustody(
      {
        getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
        getSqliteClerkTimeline,
      },
      { timelineQuery: query },
    );

    await waitFor(() => expect(getSqliteClerkTimeline).toHaveBeenCalledWith('PA1', query));
    fireEvent.click(await screen.findByRole('button', { name: /effect:enter:11/i }));

    expect(getSqliteClerkTimeline).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('Selected immutable evidence').textContent).toContain(
      'effect:enter:11',
    );
  });

  it('explains when an exact evidence filter has no matching immutable transition', async () => {
    const query: SqliteTimelineQuery = { executionId: 'missing-execution' };
    await renderCustody(
      {
        getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
        getSqliteClerkTimeline: vi.fn().mockResolvedValue(timeline({
          entries: [],
          total_entries: 0,
        })),
      },
      { timelineQuery: query },
    );

    expect(
      await screen.findByText('No immutable transitions match these exact filters.'),
    ).toBeTruthy();
    expect(screen.queryByLabelText('Selected immutable evidence')).toBeNull();
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

  it('refreshes and renders the safe-flatten plan without executing a mutation', async () => {
    const prepare = action({
      action_id: 'prepare_safe_flatten',
      label: 'Prepare safe flatten',
      explanation: 'Prepare a fresh reduction plan without submitting an order.',
      freshness: 'fresh',
      mutation: false,
      primary: false,
    });
    const checkSqliteRecoveryAction = vi.fn().mockResolvedValue({
      ...prepare,
      reduction_plan: SAFE_FLATTEN_PLAN,
      next_step: 'Review the exact attributed quantity in the plan.',
    });
    const executeSqliteRecoveryAction = vi.fn();
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([prepare])),
      checkSqliteRecoveryAction,
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: prepare.label }));

    expect(await screen.findByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeTruthy();
    expect(screen.getByText('Spy')).toBeTruthy();
    expect(screen.getByText('1.25')).toBeTruthy();
    expect(checkSqliteRecoveryAction).toHaveBeenCalledWith('PA1', prepare);
    expect(executeSqliteRecoveryAction).not.toHaveBeenCalled();
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

  it('renders the backend reason, message, and remediation on a typed refusal', async () => {
    const executeSqliteRecoveryAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason: 'stale_action_token',
            message: 'The evidence changed after this action was presented.',
            next_step: 'Refresh and review the current Clerk action.',
          },
        },
      }),
    );
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Reconcile now' }));

    expect(await screen.findByText('Stale Action Token')).toBeTruthy();
    expect(screen.getByText('The evidence changed after this action was presented.')).toBeTruthy();
    expect(screen.getByText('Refresh and review the current Clerk action.')).toBeTruthy();
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
