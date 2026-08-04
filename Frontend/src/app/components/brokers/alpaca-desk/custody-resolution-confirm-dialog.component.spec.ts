import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { CustodyDiagnosis } from '../../../api/alpaca.types';
import { CustodyResolutionConfirmDialogComponent } from './custody-resolution-confirm-dialog.component';

function divergedDiagnosis(overrides: Partial<CustodyDiagnosis> = {}): CustodyDiagnosis {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    in_sync: false,
    observed_at_ms: 1,
    snapshot_version: 'v1',
    resolution_posture: 'paper',
    resolvable: true,
    blocked_reason: null,
    divergences: [
      {
        kind: 'exposure_attribution_mismatch',
        state: 'resolvable_now',
        explanation: 'The broker holds exposure the Clerk cannot map.',
        possible_causes: ['A bot process was terminated mid-run.'],
        position_deltas: [{ symbol: 'SPY', clerk_attributed_qty: 2, broker_observed_qty: 1 }],
        resolution_step: 'record_inventory_baseline',
        prerequisite_detail: null,
        evidence_refs: [],
      },
    ],
    resolution_plan: [{ action_id: 'record_inventory_baseline', scope: 'account', mutates: true }],
    ...overrides,
  };
}

describe('CustodyResolutionConfirmDialogComponent', () => {
  it('enables confirm only with a reason and the RESOLVE token', async () => {
    const confirmed = vi.fn();
    const view = await render(CustodyResolutionConfirmDialogComponent, {
      inputs: { open: true, diagnosis: divergedDiagnosis(), busy: false, errorMessage: null },
    });
    view.fixture.componentInstance.confirmed.subscribe(confirmed);

    const confirm = screen.getByRole('button', {
      name: /resolve & sync/i,
      hidden: true,
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    fireEvent.input(screen.getByLabelText(/why did the clerk and broker/i), {
      target: { value: 'killed mid-fill' },
    });
    expect(confirm.disabled).toBe(true); // token still empty

    fireEvent.input(screen.getByLabelText(/type RESOLVE/i), { target: { value: 'RESOLVE' } });
    expect(confirm.disabled).toBe(false);

    fireEvent.click(confirm);
    expect(confirmed).toHaveBeenCalledWith({ reason: 'killed mid-fill' });
  });

  it('clears the reason and token when the dialog is closed then reopened', async () => {
    const view = await render(CustodyResolutionConfirmDialogComponent, {
      inputs: { open: true, diagnosis: divergedDiagnosis(), busy: false, errorMessage: null },
    });

    const reason = screen.getByLabelText(/why did the clerk and broker/i) as HTMLTextAreaElement;
    const token = screen.getByLabelText(/type RESOLVE/i) as HTMLInputElement;
    fireEvent.input(reason, { target: { value: 'killed mid-fill' } });
    fireEvent.input(token, { target: { value: 'RESOLVE' } });
    const confirm = screen.getByRole('button', {
      name: /resolve & sync/i,
      hidden: true,
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);

    view.fixture.componentRef.setInput('open', false);
    view.fixture.detectChanges();
    view.fixture.componentRef.setInput('open', true);
    view.fixture.detectChanges();

    expect(reason.value).toBe('');
    expect(token.value).toBe('');
    expect(confirm.disabled).toBe(true);
  });

  it('renders the delta table and recovery plan for a diverged diagnosis', async () => {
    await render(CustodyResolutionConfirmDialogComponent, {
      inputs: { open: true, diagnosis: divergedDiagnosis(), busy: false, errorMessage: null },
    });

    expect(await screen.findByText(/cannot map/i)).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText(/record inventory baseline/i)).toBeTruthy();
  });

  it('blocks confirmation with a status message while the reason is blank', async () => {
    await render(CustodyResolutionConfirmDialogComponent, {
      inputs: { open: true, diagnosis: divergedDiagnosis(), busy: false, errorMessage: null },
    });

    const reason = screen.getByLabelText(/why did the clerk and broker/i);
    expect(reason.getAttribute('aria-required')).toBe('true');
    expect(reason.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('status', { hidden: true }).textContent).toMatch(/enter a reason/i);

    fireEvent.input(reason, { target: { value: 'killed mid-fill' } });
    expect(reason.getAttribute('aria-invalid')).toBe('false');
  });

  it('emits cancelled and never emits confirmed while busy', async () => {
    const cancelled = vi.fn();
    const confirmed = vi.fn();
    const view = await render(CustodyResolutionConfirmDialogComponent, {
      inputs: { open: true, diagnosis: divergedDiagnosis(), busy: true, errorMessage: null },
    });
    view.fixture.componentInstance.cancelled.subscribe(cancelled);
    view.fixture.componentInstance.confirmed.subscribe(confirmed);

    const confirm = screen.getByRole('button', {
      name: /resolving/i,
      hidden: true,
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel', hidden: true }));
    expect(cancelled).not.toHaveBeenCalled();

    view.fixture.componentInstance.confirm();
    expect(confirmed).not.toHaveBeenCalled();
  });

  it('renders a backend errorMessage as an alert', async () => {
    await render(CustodyResolutionConfirmDialogComponent, {
      inputs: {
        open: true,
        diagnosis: divergedDiagnosis(),
        busy: false,
        errorMessage: 'The custody snapshot changed — re-check before resolving.',
      },
    });

    expect(screen.getByRole('alert', { hidden: true }).textContent).toMatch(/snapshot changed/i);
  });
});
