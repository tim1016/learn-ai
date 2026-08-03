import { HttpErrorResponse } from '@angular/common/http';
import { By } from '@angular/platform-browser';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { CustodyDiagnosis, CustodyResolutionReceipt } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';
import { CustodyResolutionConfirmDialogComponent } from './custody-resolution-confirm-dialog.component';

// jsdom (the Vitest test environment here) doesn't implement
// HTMLDialogElement.showModal()/close() at all, and the confirm dialog's
// effect guards on `typeof dialog.showModal === 'function'` before calling
// it (see custody-resolution-confirm-dialog.component.ts). Polyfill both so
// opening the dialog is observable via the native `open` attribute, matching
// the established pattern in account-desk-transaction-history.component.spec.ts
// and broker-orders.component.spec.ts.
if (typeof HTMLDialogElement.prototype.showModal !== 'function') {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
}
if (typeof HTMLDialogElement.prototype.close !== 'function') {
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
}

function diagnosis(overrides: Partial<CustodyDiagnosis> = {}): CustodyDiagnosis {
  return {
    broker: 'alpaca', account_id: 'PA1', in_sync: true, observed_at_ms: 1,
    snapshot_version: 'v1', resolution_posture: 'paper', resolvable: false,
    blocked_reason: null, divergences: [], resolution_plan: [], ...overrides,
  } as CustodyDiagnosis;
}

function divergedResolvable(): CustodyDiagnosis {
  return diagnosis({
    in_sync: false,
    resolvable: true,
    snapshot_version: 'snap-1',
    divergences: [{
      kind: 'exposure_attribution_mismatch', state: 'resolvable_now',
      explanation: 'The broker holds exposure the Clerk cannot map.',
      possible_causes: ['A bot process was terminated mid-run.'],
      position_deltas: [{ symbol: 'SPY', clerk_attributed_qty: 2, broker_observed_qty: 1 }],
      resolution_step: 'record_inventory_baseline', prerequisite_detail: null, evidence_refs: [],
    }],
    resolution_plan: [{ action_id: 'record_inventory_baseline', scope: 'account', mutates: true }],
  });
}

function receiptOf(overrides: Partial<CustodyResolutionReceipt> = {}): CustodyResolutionReceipt {
  return {
    broker: 'alpaca', account_id: 'PA1', resolved: true, receipt_id: 'receipt-1',
    recorded_at_ms: 2, in_sync: true,
    steps_executed: [{ action_id: 'record_inventory_baseline', message: 'Baseline recorded.' }],
    remaining_divergences: [],
    ...overrides,
  } as CustodyResolutionReceipt;
}

function svc(d: CustodyDiagnosis) {
  return { getCustodyDiagnosis: vi.fn().mockResolvedValue(d) };
}

describe('AlpacaCustodyResolutionComponent', () => {
  it('shows the in-sync strip when clerk and broker agree', async () => {
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: svc(diagnosis()) }],
    });
    expect(await screen.findByText(/in sync/i)).toBeTruthy();
  });

  it('shows the delta and explanation when diverged', async () => {
    const diverged = diagnosis({
      in_sync: false, resolvable: true,
      divergences: [{
        kind: 'exposure_attribution_mismatch', state: 'resolvable_now',
        explanation: 'The broker holds exposure the Clerk cannot map.',
        possible_causes: ['A bot process was terminated mid-run.'],
        position_deltas: [{ symbol: 'SPY', clerk_attributed_qty: 2, broker_observed_qty: 1 }],
        resolution_step: 'record_inventory_baseline', prerequisite_detail: null, evidence_refs: [],
      }],
      resolution_plan: [{ action_id: 'record_inventory_baseline', scope: 'account', mutates: true }],
    });
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: svc(diverged) }],
    });
    expect(await screen.findByText(/cannot map/i)).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByRole('button', { name: /resolve & sync/i })).toBeTruthy();
  });

  it('opens the confirm dialog when "Resolve & sync" is clicked', async () => {
    const { fixture } = await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: svc(divergedResolvable()) }],
    });
    await screen.findByRole('button', { name: /resolve & sync/i });

    const trigger = fixture.nativeElement.querySelector(
      'button.custody__resolve',
    ) as HTMLButtonElement;
    trigger.click();
    fixture.detectChanges();

    const dialog = fixture.nativeElement.querySelector('dialog');
    expect(dialog?.hasAttribute('open')).toBe(true);
  });

  it('confirming calls resolveCustody with the snapshot_version + RESOLVE token, renders the receipt, and re-fetches the diagnosis', async () => {
    const getCustodyDiagnosis = vi.fn().mockResolvedValue(divergedResolvable());
    const resolveCustody = vi.fn().mockResolvedValue(receiptOf());
    const { fixture } = await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: { getCustodyDiagnosis, resolveCustody } }],
    });
    await screen.findByRole('button', { name: /resolve & sync/i });

    const trigger = fixture.nativeElement.querySelector(
      'button.custody__resolve',
    ) as HTMLButtonElement;
    trigger.click();
    fixture.detectChanges();

    const dialogDebug = fixture.debugElement.query(
      By.directive(CustodyResolutionConfirmDialogComponent),
    );
    dialogDebug.componentInstance.confirmed.emit({
      reason: 'A bot process was terminated mid-run.',
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(resolveCustody).toHaveBeenCalledWith('alpaca', {
      reason: 'A bot process was terminated mid-run.',
      snapshot_version: 'snap-1',
      confirmation_token: 'RESOLVE',
      idempotency_key: expect.any(String),
    });
    expect(getCustodyDiagnosis).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/baseline recorded/i)).toBeTruthy();
    expect(screen.getByText('receipt-1')).toBeTruthy();
    // The dialog unmounts once the receipt is in (confirmOpen flips false).
    expect(fixture.nativeElement.querySelector('dialog')).toBeNull();
  });

  it('re-diagnoses and shows an honest "state changed" message on a 409, without a receipt', async () => {
    const getCustodyDiagnosis = vi.fn().mockResolvedValue(divergedResolvable());
    const resolveCustody = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { detail: { message: 'Snapshot changed.', why: 'Re-check before resolving.' } },
      }),
    );
    const { fixture } = await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: { getCustodyDiagnosis, resolveCustody } }],
    });
    await screen.findByRole('button', { name: /resolve & sync/i });

    const trigger = fixture.nativeElement.querySelector(
      'button.custody__resolve',
    ) as HTMLButtonElement;
    trigger.click();
    fixture.detectChanges();

    const dialogDebug = fixture.debugElement.query(
      By.directive(CustodyResolutionConfirmDialogComponent),
    );
    dialogDebug.componentInstance.confirmed.emit({
      reason: 'A bot process was terminated mid-run.',
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(getCustodyDiagnosis).toHaveBeenCalledTimes(2);
    expect(dialogDebug.componentInstance.errorMessage()).toMatch(/state changed/i);
    expect(fixture.nativeElement.querySelector('app-panel-action-receipt')).toBeNull();
    // Never auto-resubmit: only the one resolveCustody call from the confirm above.
    expect(resolveCustody).toHaveBeenCalledTimes(1);
  });

  it('does not resurface a stale resolve error when the dialog is reopened after cancel', async () => {
    const getCustodyDiagnosis = vi.fn().mockResolvedValue(divergedResolvable());
    const resolveCustody = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { detail: { message: 'Snapshot changed.', why: 'Re-check before resolving.' } },
      }),
    );
    const { fixture } = await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: { getCustodyDiagnosis, resolveCustody } }],
    });
    await screen.findByRole('button', { name: /resolve & sync/i });

    const trigger = fixture.nativeElement.querySelector(
      'button.custody__resolve',
    ) as HTMLButtonElement;
    trigger.click();
    fixture.detectChanges();

    let dialogDebug = fixture.debugElement.query(
      By.directive(CustodyResolutionConfirmDialogComponent),
    );
    dialogDebug.componentInstance.confirmed.emit({
      reason: 'A bot process was terminated mid-run.',
    });
    await fixture.whenStable();
    fixture.detectChanges();

    // Confirm the error actually surfaced before cancelling — otherwise this
    // test would pass vacuously.
    dialogDebug = fixture.debugElement.query(
      By.directive(CustodyResolutionConfirmDialogComponent),
    );
    expect(dialogDebug.componentInstance.errorMessage()).toMatch(/state changed/i);

    dialogDebug.componentInstance.cancelled.emit();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('dialog')).toBeNull();

    // Re-query the trigger: the diagnosis.reload() triggered by the 409 above
    // round-trips isLoading(), which tears down and rebuilds the diverged
    // `@else if` branch — the originally-captured button node is now
    // detached from the live view.
    const reopenTrigger = fixture.nativeElement.querySelector(
      'button.custody__resolve',
    ) as HTMLButtonElement;
    reopenTrigger.click();
    await fixture.whenStable();
    fixture.detectChanges();

    dialogDebug = fixture.debugElement.query(
      By.directive(CustodyResolutionConfirmDialogComponent),
    );
    expect(dialogDebug).not.toBeNull();
    expect(dialogDebug.componentInstance.errorMessage()).toBeNull();
    // No new submission has happened yet — the reopen alone must not carry
    // the prior attempt's error forward.
    expect(resolveCustody).toHaveBeenCalledTimes(1);
  });
});
