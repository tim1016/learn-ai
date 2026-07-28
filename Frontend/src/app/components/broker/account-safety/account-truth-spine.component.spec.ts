import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { makeAccountSafetySnapshot } from '../../../../testing/account-safety-snapshot-fixtures';
import { AccountTruthSpineComponent } from './account-truth-spine.component';

describe('AccountTruthSpineComponent', () => {
  it('renders the server-owned verdict, epoch, stale source, and exposure without deriving clean state', async () => {
    await render(AccountTruthSpineComponent, {
      inputs: { accountState: { state: 'fresh', snapshot: makeAccountSafetySnapshot() } },
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
    });

    expect(screen.getByText(/No local clean verdict has been inferred/)).toBeTruthy();
  });
});
