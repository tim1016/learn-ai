import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { ClerkStatus, SqliteClerkProjection } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { healthyAccountOperatorPostureFixture } from '../../../testing/operator-blocker-fixtures';
import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';

function clerkStatus(): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    hold: { active: false, reason_code: null, reason: null, since_ms: null },
    latest_reconciliation: null,
    outstanding_intents: 0,
    observed_at_ms: 1,
    authority_kind: 'real_paper',
    operator_posture: healthyAccountOperatorPostureFixture(),
  };
}

function projection(): SqliteClerkProjection {
  return {
    account_id: 'PA1', strategy_instance_id: null, authority_generation: 1,
    db_identity_token: 'db-1', authority_health: 'healthy', authority_health_reason: null,
    control_revision: 1, custody_owner: 'ACCOUNT_CLERK', runs: [], commands: [], operations: [],
    positions: [], holds: [], uncertainties: [], latest_reconciliation: null, terminal_receipts: [],
    guidance: {
      headline: 'Account Clerk custody is healthy',
      explanation: 'Durable Clerk state has no unresolved uncertainty.', scope: 'ACCOUNT_CLERK',
      impact: 'Normal Clerk-governed controls remain available.', custody_owner: 'ACCOUNT_CLERK',
      may_create_exposure: true, available_safety_actions: [], action_required: false,
      next_step: 'No recovery action is required.',
    },
    recovery_actions: [], generated_at_ms: 1,
  };
}

describe('AlpacaOperatorLensDataService', () => {
  it('reloads Clerk status alongside the SQLite projection on refreshProjection', async () => {
    const getClerkStatus = vi.fn().mockResolvedValue(clerkStatus());
    const getSqliteClerkProjection = vi.fn().mockResolvedValue(projection());

    TestBed.configureTestingModule({
      providers: [
        AlpacaOperatorLensDataService,
        { provide: BrokersService, useValue: { getClerkStatus, getSqliteClerkProjection } },
      ],
    });
    const service = TestBed.inject(AlpacaOperatorLensDataService);

    service.loadOnce();
    await vi.waitFor(() => expect(getClerkStatus).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledTimes(1));
    expect(getClerkStatus).toHaveBeenCalledWith('alpaca');

    service.refreshProjection();

    await vi.waitFor(() => expect(getClerkStatus).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledTimes(2));
  });

  it('never fetches before loadOnce is called', async () => {
    const getClerkStatus = vi.fn().mockResolvedValue(clerkStatus());
    const getSqliteClerkProjection = vi.fn().mockResolvedValue(projection());

    TestBed.configureTestingModule({
      providers: [
        AlpacaOperatorLensDataService,
        { provide: BrokersService, useValue: { getClerkStatus, getSqliteClerkProjection } },
      ],
    });
    TestBed.inject(AlpacaOperatorLensDataService);

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(getClerkStatus).not.toHaveBeenCalled();
    expect(getSqliteClerkProjection).not.toHaveBeenCalled();
  });
});
