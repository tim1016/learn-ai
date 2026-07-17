import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { AccountDeskRecoveryConfirmDialogComponent } from './account-desk-recovery-confirm-dialog.component';

describe('AccountDeskRecoveryConfirmDialogComponent', () => {
  it('renders the exact confirmation, requires an exposure reason, and supports cancellation', async () => {
    const view = await render(AccountDeskRecoveryConfirmDialogComponent, {
      inputs: {
        confirmation: {
          command: 'exposure_override', accountId: 'DU1234567', title: 'Accept account exposure', body: 'Backend body.',
          consequence: 'Backend consequence.', confirmLabel: 'Accept exposure', requiredToken: '', providedToken: '', desiredAutomationEnabled: null, reason: '', journalCure: null, legacyCandidate: null, recoveryFlatten: null,
        },
        busy: false,
        errorMessage: null,
      },
    });
    const cancelled = vi.fn();
    const reasonChanged = vi.fn();
    view.fixture.componentInstance.cancelled.subscribe(cancelled);
    view.fixture.componentInstance.exposureReasonChanged.subscribe(reasonChanged);

    expect(await screen.findByText('Backend consequence.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Accept exposure', hidden: true }).hasAttribute('disabled')).toBe(true);
    const reason = screen.getByLabelText('Why are you accepting this account exposure?');
    expect(reason.getAttribute('aria-required')).toBe('true');
    expect(reason.getAttribute('aria-describedby')).toBe('account-exposure-override-reason-help');
    expect(screen.getByText('Enter an operator reason to enable Accept exposure.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Close confirmation', hidden: true })).toBeTruthy();
    fireEvent.input(reason, { target: { value: 'Operator reason.' } });
    expect(reasonChanged).toHaveBeenCalledWith('Operator reason.');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel', hidden: true }));
    expect(cancelled).toHaveBeenCalledOnce();
    expect(document.querySelector('dialog.account-recovery-confirm')).not.toBeNull();
  });

  it('treats the native dialog cancel event as cancellation', async () => {
    const view = await render(AccountDeskRecoveryConfirmDialogComponent, {
      inputs: {
        confirmation: {
          command: 'reconcile', accountId: 'DU1234567', title: 'Run account reconciliation', body: 'Backend body.',
          consequence: 'Backend consequence.', confirmLabel: 'Run account reconcile', requiredToken: '', providedToken: '', desiredAutomationEnabled: null, reason: '', journalCure: null, legacyCandidate: null, recoveryFlatten: null,
        },
        busy: false,
        errorMessage: null,
      },
    });
    const cancelled = vi.fn();
    view.fixture.componentInstance.cancelled.subscribe(cancelled);
    const dialog = document.querySelector<HTMLDialogElement>('dialog.account-recovery-confirm');
    if (dialog === null) throw new Error('Expected recovery confirmation dialog.');

    fireEvent(dialog, new Event('cancel', { cancelable: true }));

    expect(cancelled).toHaveBeenCalledOnce();
  });

  it('renders and accepts an exact backend-required confirmation token', async () => {
    const view = await render(AccountDeskRecoveryConfirmDialogComponent, {
      inputs: {
        confirmation: {
          command: 'reconcile', accountId: 'DU1234567', title: 'Run account reconciliation', body: 'Backend body.',
          consequence: 'Backend consequence.', confirmLabel: 'Run account reconcile', requiredToken: 'HALT', providedToken: 'HALT', desiredAutomationEnabled: null, reason: '', journalCure: null, legacyCandidate: null, recoveryFlatten: null,
        },
        busy: false,
        errorMessage: null,
      },
    });
    const confirmed = vi.fn();
    view.fixture.componentInstance.confirmed.subscribe(confirmed);

    expect(screen.getByLabelText('Type HALT to confirm')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Run account reconcile', hidden: true }).hasAttribute('disabled')).toBe(false);
    view.fixture.componentInstance.confirm();
    expect(confirmed).toHaveBeenCalledOnce();
  });
});
