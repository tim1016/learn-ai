import { Router } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { OperatorBlocker } from '../../../api/operator-blocker.types';
import { AccountDeskGuidanceComponent } from './account-desk-guidance.component';
import { AccountDeskGuidanceStore } from './account-desk-guidance-store.service';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';

describe('AccountDeskGuidanceComponent', () => {
  it('sends a backend-declared inline move to the recovery confirmation store', async () => {
    const recovery = { requestDeclaredMove: vi.fn() };
    await render(AccountDeskGuidanceComponent, {
      inputs: { anchor: 'reconciliation', lens: 'operator' },
      providers: [
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([blocker()]) } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Run account reconcile' }));
    expect(recovery.requestDeclaredMove).toHaveBeenCalledWith(expect.objectContaining({ blocker: blocker() }));
  });
});

function blocker(): OperatorBlocker {
  return {
    condition: { id: 'reconcile-1', severity: 'blocking', scope: 'account', evidence: {} },
    host: 'account_desk', anchor: { kind: 'reconciliation', subject_key: null }, audience: 'operator', disposition: 'fix_here',
    headline: 'Backend-authored reconciliation', detail: 'Backend-authored detail', applies_to: 'both', secondary_moves: [],
    primary_move: {
      label: 'Run account reconcile', target: null, action: { kind: 'confirm_in_form', anchor: 'account-reconciliation-action' },
      confirmation: { title: 'Run account reconciliation', body: 'Backend body', consequence: 'Backend consequence', confirm_label: 'Run account reconcile', required_token: '' },
    },
  };
}
