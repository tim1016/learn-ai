import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountReconciliationReceipt } from '../../../api/account-reconciliation.types';
import type { OperatorBlockerMoveEvent } from '../shared/operator-blocker-list/operator-blocker-list.component';
import { BrokerService } from '../../../services/broker.service';
import { AccountDeskEventsStore } from './account-desk-events-store.service';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

describe('AccountDeskRecoveryStore', () => {
  const broker = {
    reconcileAccount: vi.fn(),
    updateAccountReconciliationAutomation: vi.fn(),
    clearAccountFreeze: vi.fn(),
    acceptExposureOverride: vi.fn(),
  };
  const surface = { load: vi.fn() };
  const events = { load: vi.fn() };

  beforeEach(() => {
    Object.values(broker).forEach((method) => method.mockReset());
    surface.load.mockReset().mockResolvedValue(undefined);
    events.load.mockReset().mockResolvedValue(undefined);
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskRecoveryStore,
        { provide: BrokerService, useValue: broker },
        { provide: AccountDeskSurfaceStore, useValue: surface },
        { provide: AccountDeskEventsStore, useValue: events },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('opens only a server-declared account-desk confirmation and cancellation sends no mutation', () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    store.load('DU1234567');

    store.requestDeclaredMove(move('account-reconciliation-action'));
    expect(store.confirmation()?.command).toBe('reconcile');
    store.cancelConfirmation();
    expect(store.confirmation()).toBeNull();
    expect(broker.reconcileAccount).not.toHaveBeenCalled();

    store.requestDeclaredMove(move('unknown-account-action'));
    expect(store.confirmation()).toBeNull();
  });

  it('preserves a successful reconciliation receipt and refreshes proof and timeline', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.reconcileAccount.mockResolvedValue(receipt());
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-reconciliation-action'));

    await store.confirm();

    expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    expect(store.success()?.kind).toBe('reconcile');
    expect(surface.load).toHaveBeenCalledWith('DU1234567');
    expect(events.load).toHaveBeenCalledWith('DU1234567');
  });

  it('confirms the exact backend policy change before sending it', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.updateAccountReconciliationAutomation.mockResolvedValue({
      account_id: 'DU1234567', enabled: true, updated_at_ms: 1_780_000_000_000, updated_by: 'account-desk.operator',
    });
    store.load('DU1234567');
    store.requestAutomationChange({
      schema_version: 1, account_id: 'DU1234567', enabled: false, updated_at_ms: 0, updated_by: 'system.default',
    });

    expect(store.confirmation()?.desiredAutomationEnabled).toBe(true);
    await store.confirm();

    expect(broker.updateAccountReconciliationAutomation).toHaveBeenCalledWith('DU1234567', {
      enabled: true, updated_by: 'account-desk.operator',
    });
  });

  it('requires an operator-entered reason for a declared exposure override', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.acceptExposureOverride.mockResolvedValue({
      schema_version: 1, account_id: 'DU1234567', cleared: true, cleared_source: 'account_audited_override', override_id: 'override-1', triage: {},
    });
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-exposure-override-action'));

    expect(store.canConfirm()).toBe(false);
    store.setExposureOverrideReason('Operator reviewed the projected exposure.');
    await store.confirm();

    expect(broker.acceptExposureOverride).toHaveBeenCalledWith('DU1234567', {
      requested_by: 'account-desk.operator', reason: 'Operator reviewed the projected exposure.',
    });
  });

  it('keeps a rejected recovery visibly distinct from success without refreshing the verdict', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.clearAccountFreeze.mockRejectedValue(new Error('rejected'));
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-clear-freeze-action'));

    await store.confirm();

    expect(store.success()).toBeNull();
    expect(store.errorMessage()).toBe('Account recovery was not accepted. Review the current proof and try again.');
    expect(surface.load).not.toHaveBeenCalled();
    expect(events.load).not.toHaveBeenCalled();
  });

  it('does not attach a late mutation result to a newly selected account', async () => {
    const deferred = promise<AccountReconciliationReceipt>();
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.reconcileAccount.mockReturnValue(deferred.promise);
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-reconciliation-action'));
    const confirmation = store.confirm();
    store.load('DU7654321');
    deferred.resolve(receipt());
    await confirmation;

    expect(store.success()).toBeNull();
    expect(surface.load).not.toHaveBeenCalledWith('DU7654321');
  });
});

function move(anchor: string): OperatorBlockerMoveEvent {
  return {
    blocker: {
      condition: { id: 'condition-1', severity: 'blocking', scope: 'account', evidence: {} },
      host: 'account_desk', anchor: { kind: 'cure_tools', subject_key: null }, audience: 'operator', disposition: 'fix_here',
      headline: 'Backend authored headline', detail: 'Backend authored detail', primary_move: null, secondary_moves: [], applies_to: 'both',
    },
    move: {
      label: 'Backend authored move', action: { kind: 'confirm_in_form', anchor }, target: null,
      confirmation: {
        title: 'Backend authored title', body: 'Backend authored body', consequence: 'Backend authored consequence', confirm_label: 'Confirm', required_token: '',
      },
    },
  };
}

function receipt(): AccountReconciliationReceipt {
  return {
    schema_version: 1, receipt_id: 'receipt-1', account_id: 'DU1234567', requested_account_id: 'DU1234567', connected_account_id: 'DU1234567',
    state: 'CLEAN', account_truth_verdict: 'clean', account_truth_severity: 'ok',
    final_gate_result: { gate_id: 'account.reconcile', status: 'pass', source: 'test', operator_reason: 'Backend receipt', operator_next_step: 'NONE', evidence_at_ms: 1_780_000_000_000 },
    exposure_resolution: 'flat', account_truth: {} as AccountReconciliationReceipt['account_truth'], evidence_refs: [],
    generated_at_ms: 1_780_000_000_000, account_truth_generated_at_ms: 1_780_000_000_000, expires_at_ms: 1_780_000_060_000, ttl_ms: 60_000,
  };
}

function promise<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const pending = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise: pending, resolve };
}
