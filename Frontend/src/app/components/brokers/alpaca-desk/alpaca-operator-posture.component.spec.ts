import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter, Router } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import type { SqliteClerkProjection, SqliteRecoveryAction } from '../../../api/alpaca.types';
import { operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';
import { AlpacaOperatorPostureComponent } from './alpaca-operator-posture.component';

function action(overrides: Partial<SqliteRecoveryAction> = {}): SqliteRecoveryAction {
  return {
    action_id: 'reconcile_now',
    label: 'Reconcile now',
    explanation: 'Compare Clerk custody to a fresh broker observation.',
    available: true,
    unavailable_reason_code: null,
    unavailable_reason: null,
    scope: 'ACCOUNT_CLERK',
    freshness: 'fresh',
    evidence: [],
    reduction_plan: null,
    confirmation: null,
    next_step: 'Run reconciliation against the current observation.',
    concurrency_token: 'token-1',
    execution_ref: null,
    mutation: true,
    primary: true,
    ...overrides,
  };
}

function projection(overrides: Partial<SqliteClerkProjection> = {}): SqliteClerkProjection {
  return {
    account_id: 'PA1',
    strategy_instance_id: null,
    authority_generation: 1,
    db_identity_token: 'db-1',
    authority_health: 'healthy',
    authority_health_reason: null,
    control_revision: 3,
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
      headline: 'Clerk reconciliation required',
      explanation: 'The Clerk needs a fresh broker observation.',
      scope: 'ACCOUNT_CLERK',
      impact: 'No new exposure may be created.',
      custody_owner: 'ACCOUNT_CLERK',
      may_create_exposure: false,
      available_safety_actions: ['Reconcile now'],
      action_required: true,
      next_step: 'Run reconciliation.',
    },
    recovery_actions: [action()],
    generated_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

describe('AlpacaOperatorPostureComponent', () => {
  it('renders one healthy posture from server-authored Clerk guidance', async () => {
    await render(AlpacaOperatorPostureComponent, {
      inputs: {
        projection: projection({ guidance: { ...projection().guidance, action_required: false } }),
      },
    });

    expect(screen.getByRole('heading', { name: 'Clerk reconciliation required' })).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('does not claim health when unresolved uncertainty remains without a required action', async () => {
    await render(AlpacaOperatorPostureComponent, {
      inputs: {
        projection: projection({
          guidance: {
            ...projection().guidance,
            headline: 'Execution evidence needs review',
            explanation: 'One broker outcome is not yet proven.',
            action_required: false,
          },
          recovery_actions: [],
          uncertainties: [{
            uncertainty_id: 'uncertainty-1',
            scope: 'ACCOUNT_CLERK',
            strategy_instance_id: null,
            reason_code: 'BROKER_OUTCOME_UNPROVEN',
            severity: 'warning',
            headline: 'Execution evidence needs review',
            explanation: 'One broker outcome is not yet proven.',
            operator_impact: 'Review before relying on the account record.',
            blocks_new_exposure: false,
            allows_reduction: true,
            next_step: 'Review the order evidence.',
            custody_owner: 'ACCOUNT_CLERK',
            observed_at_ms: 1_700_000_000_000,
            evidence_age_ms: 10,
            evidence_refs: ['order-1'],
          }],
        }),
      },
    });

    expect(screen.getByRole('heading', { name: 'Execution evidence needs review' })).toBeTruthy();
    expect(screen.getByText('Review the current evidence.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'System healthy' })).toBeNull();
  });

  it('keeps an available recovery action in review when server guidance does not require it', async () => {
    const repairRequested = vi.fn();
    await render(AlpacaOperatorPostureComponent, {
      inputs: {
        projection: projection({
          guidance: {
            ...projection().guidance,
            action_required: false,
          },
          uncertainties: [{
            uncertainty_id: 'uncertainty-1',
            scope: 'ACCOUNT_CLERK',
            strategy_instance_id: null,
            reason_code: 'BROKER_OUTCOME_UNPROVEN',
            severity: 'warning',
            headline: 'Execution evidence needs review',
            explanation: 'One broker outcome is not yet proven.',
            operator_impact: 'Review before relying on the account record.',
            blocks_new_exposure: false,
            allows_reduction: true,
            next_step: 'Review the order evidence.',
            custody_owner: 'ACCOUNT_CLERK',
            observed_at_ms: 1_700_000_000_000,
            evidence_age_ms: 10,
            evidence_refs: ['order-1'],
          }],
        }),
      },
      on: { repairRequested },
    });

    expect(screen.getByText('Review the current evidence.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Reconcile now' })).toBeNull();
    expect(repairRequested).not.toHaveBeenCalled();
  });

  it('attaches the available repair action to the dominant Clerk posture', async () => {
    const repairRequested = vi.fn();
    await render(AlpacaOperatorPostureComponent, {
      inputs: { projection: projection() },
      on: { repairRequested },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile now' }));

    expect(repairRequested).toHaveBeenCalledWith(expect.objectContaining({ action_id: 'reconcile_now' }));
  });

  it('renders the exact OperatorBlocker move for a fix-elsewhere disposition', async () => {
    const view = await render(AlpacaOperatorPostureComponent, {
      inputs: {
        blocker: operatorBlockerFixture({
          disposition: 'fix_elsewhere',
          headline: 'Broker connection needs attention',
        }),
      },
      providers: [provideRouter([])],
    });

    const router = view.fixture.debugElement.injector.get(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    fireEvent.click(screen.getByRole('button', { name: 'Connect the broker' }));

    expect(navigate).toHaveBeenCalledWith(['/broker'], { fragment: undefined });
  });

  it.each([
    ['wait', 'Waiting for fresh account evidence.'],
    ['terminal', 'Manual escalation required.'],
  ] as const)('uses the %s OperatorBlocker disposition without inventing a repair', async (disposition, copy) => {
    await render(AlpacaOperatorPostureComponent, {
      inputs: { blocker: operatorBlockerFixture({ disposition, primaryMove: null }) },
    });

    expect(screen.getByText(copy)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
});
