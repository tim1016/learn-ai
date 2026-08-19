import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { BrokerService } from '../../../services/broker.service';
import { makePresentedReconcileAction } from '../../../../testing/account-safety-snapshot-fixtures';
import { AccountSafetySnapshotStore } from './account-safety-snapshot.store';
import { PresentedAccountActionComponent } from './presented-account-action.component';

describe('PresentedAccountActionComponent', () => {
  it('dispatches only the server-presented reconcile envelope and refreshes its account', async () => {
    const action = makePresentedReconcileAction();
    const broker = {
      executePresentedReconcileNow: vi.fn().mockResolvedValue({
        action_id: 'reconcile_now',
        action_attempt_id: action.idempotency_key,
        state: 'ACCEPTED',
        replayed: false,
        finished_copy: action.finished_copy,
        reconciliation_receipt: null,
        refreshed_snapshot_id: 'd'.repeat(64),
      }),
    };
    const accountSafety = { refresh: vi.fn().mockResolvedValue(undefined) };
    await render(PresentedAccountActionComponent, {
      inputs: { action },
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: AccountSafetySnapshotStore, useValue: accountSafety },
      ],
    });

    expect(screen.getByText(action.confirmation.body)).toBeTruthy();
    expect(screen.getByText(action.confirmation.consequence)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: action.confirmation.confirm_label }));

    await waitFor(() => {
      expect(broker.executePresentedReconcileNow).toHaveBeenCalledWith(action.target.account_id, action);
    });
    expect(accountSafety.refresh).toHaveBeenCalledWith(action.target.account_id);
    expect(screen.getByText(action.finished_copy, { exact: false })).toBeTruthy();
  });

  it('does not dispatch an unavailable action', async () => {
    const action = { ...makePresentedReconcileAction(), availability: 'UNAVAILABLE' as const, disposition: 'wait' as const };
    const broker = { executePresentedReconcileNow: vi.fn() };
    await render(PresentedAccountActionComponent, {
      inputs: { action },
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: AccountSafetySnapshotStore, useValue: { refresh: vi.fn() } },
      ],
    });

    const button = screen.getByRole('button', { name: action.confirmation.confirm_label });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(broker.executePresentedReconcileNow).not.toHaveBeenCalled();
  });

  it('renders the server-owned unknown-outcome copy without treating it as a failure', async () => {
    const action = makePresentedReconcileAction();
    const unknownCopy = 'The reconciliation request was durably claimed, but its observed outcome is not proven.';
    const broker = {
      executePresentedReconcileNow: vi.fn().mockResolvedValue({
        action_id: 'reconcile_now',
        action_attempt_id: action.idempotency_key,
        state: 'OUTCOME_UNKNOWN',
        replayed: false,
        finished_copy: unknownCopy,
        reconciliation_receipt: null,
        refreshed_snapshot_id: null,
      }),
    };
    await render(PresentedAccountActionComponent, {
      inputs: { action },
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: AccountSafetySnapshotStore, useValue: { refresh: vi.fn().mockResolvedValue(undefined) } },
      ],
    });

    fireEvent.click(screen.getByRole('button', { name: action.confirmation.confirm_label }));

    expect(await screen.findByText(unknownCopy, { exact: false })).toBeTruthy();
    expect(screen.getByText('Outcome Unknown', { exact: false })).toBeTruthy();
  });

  it('renders a typed server refusal verbatim and refreshes the evidence', async () => {
    const action = makePresentedReconcileAction();
    const rejection = {
      reason_code: 'ACTION_SNAPSHOT_STALE',
      message: 'Account safety evidence changed; refresh before retrying.',
      snapshot_id: 'd'.repeat(64),
      snapshot_version: 'd'.repeat(64),
    };
    const broker = {
      executePresentedReconcileNow: vi.fn().mockRejectedValue(
        new HttpErrorResponse({ status: 409, error: { detail: rejection } }),
      ),
    };
    const accountSafety = { refresh: vi.fn().mockResolvedValue(undefined) };
    await render(PresentedAccountActionComponent, {
      inputs: { action },
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: AccountSafetySnapshotStore, useValue: accountSafety },
      ],
    });

    fireEvent.click(screen.getByRole('button', { name: action.confirmation.confirm_label }));

    expect(await screen.findByText(rejection.message, { exact: false })).toBeTruthy();
    expect(screen.getByText('Action Snapshot Stale', { exact: false })).toBeTruthy();
    expect(screen.queryByText('Request Outcome Unavailable', { exact: false })).toBeNull();
    expect(accountSafety.refresh).toHaveBeenCalledWith(action.target.account_id);
  });

  it('keeps the recorded broker result and releases the control when refresh fails', async () => {
    const action = makePresentedReconcileAction();
    const broker = {
      executePresentedReconcileNow: vi.fn().mockResolvedValue({
        action_id: 'reconcile_now',
        action_attempt_id: action.idempotency_key,
        state: 'ACCEPTED',
        replayed: false,
        finished_copy: action.finished_copy,
        reconciliation_receipt: null,
        refreshed_snapshot_id: null,
      }),
    };
    const accountSafety = { refresh: vi.fn().mockRejectedValue(new Error('refresh unavailable')) };
    await render(PresentedAccountActionComponent, {
      inputs: { action },
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: AccountSafetySnapshotStore, useValue: accountSafety },
      ],
    });

    const button = screen.getByRole('button', { name: action.confirmation.confirm_label });
    fireEvent.click(button);

    expect(await screen.findByText(action.finished_copy, { exact: false })).toBeTruthy();
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByText('could not refresh', { exact: false })).toBeTruthy();
  });
});
