import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { BrokerService } from '../../../services/broker.service';
import { makeAccountSafetySnapshot, makePresentedReconcileAction } from '../../../../testing/account-safety-snapshot-fixtures';
import { AccountSafetySnapshotStore } from './account-safety-snapshot.store';
import { AccountTruthSpineComponent } from './account-truth-spine.component';

describe('AccountTruthSpineComponent', () => {
  const providers = [
    { provide: BrokerService, useValue: { executePresentedReconcileNow: vi.fn() } },
    { provide: AccountSafetySnapshotStore, useValue: { refresh: vi.fn() } },
  ];

  it('renders the server-owned verdict, epoch, stale source, and exposure without deriving clean state', async () => {
    await render(AccountTruthSpineComponent, {
      inputs: { accountState: { state: 'fresh', snapshot: makeAccountSafetySnapshot() } },
      providers,
    });

    expect(screen.getByText('Reconciling')).toBeTruthy();
    expect(screen.getByText('Fresh reconciliation is required before new entry risk can proceed.')).toBeTruthy();
    expect(screen.getByText('Unmanaged / unknown')).toBeTruthy();
    expect(screen.getByText('1')).toBeTruthy();
    expect(screen.getByText(/Browser online · local evidence only/)).toBeTruthy();
  });

  it('shows unavailable evidence honestly when no server snapshot exists', async () => {
    await render(AccountTruthSpineComponent, {
      inputs: { accountState: { state: 'unavailable', snapshot: null } },
      providers,
    });

    expect(screen.getByText(/No local clean verdict has been inferred/)).toBeTruthy();
  });

  it('hosts the closed account action returned by the safety snapshot', async () => {
    const action = makePresentedReconcileAction();
    await render(AccountTruthSpineComponent, {
      inputs: { accountState: { state: 'fresh', snapshot: makeAccountSafetySnapshot({ actions: [action] }) } },
      providers,
    });

    expect(screen.getByRole('button', { name: action.confirmation.confirm_label })).toBeTruthy();
  });
});
