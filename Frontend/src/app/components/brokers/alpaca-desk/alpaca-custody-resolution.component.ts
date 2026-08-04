import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';

import type { CustodyDivergence, CustodyResolutionReceipt } from '../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { BrokersService } from '../../../services/brokers.service';
import {
  type ActionReceiptView,
  PanelActionReceiptComponent,
} from '../../broker/v2-panel/panel-shell/panel-action-receipt.component';
import { CustodyDivergenceComponent } from './custody-divergence.component';
import { CustodyResolutionConfirmDialogComponent } from './custody-resolution-confirm-dialog.component';

const STATE_CHANGED_MESSAGE = 'Account state changed since you looked — re-checking.';

/**
 * Alpaca custody-resolution card. Slice 1 (read-only) renders the
 * backend-authored Clerk↔broker custody diagnosis across four states:
 * loading, error, in-sync, and diverged, surfacing each divergence's
 * explanation, position deltas, and possible causes verbatim. Slice 2
 * (Task 2.4) wires the mutating "Resolve & sync" flow: the button opens the
 * typed-confirmation dialog (Task 2.3); confirming calls
 * `POST /clerk/resolve` (Task 2.2) and renders the resulting receipt. A 409
 * (stale snapshot / blocked prerequisite) re-diagnoses but never auto-fires a
 * second submission — the operator re-confirms explicitly against fresh
 * state, mirroring the confirmed-action policy in `BrokerV2PanelService`.
 */
@Component({
  selector: 'app-alpaca-custody-resolution',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    TimestampDisplayComponent,
    CustodyDivergenceComponent,
    CustodyResolutionConfirmDialogComponent,
    PanelActionReceiptComponent,
  ],
  templateUrl: './alpaca-custody-resolution.component.html',
  styleUrl: './alpaca-custody-resolution.component.scss',
  host: { class: 'block' },
})
export class AlpacaCustodyResolutionComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly diagnosis = resource({
    loader: () => this.brokers.getCustodyDiagnosis('alpaca'),
  });

  protected readonly confirmOpen = signal(false);
  protected readonly busy = signal(false);
  protected readonly resolveError = signal<string | null>(null);
  protected readonly receipt = signal<ActionReceiptView | null>(null);

  // `divergences` is optional (`?:`) on the generated CustodyDiagnosis schema
  // (default `[]` server-side); normalize here so the template never has to
  // reason about `undefined`.
  protected readonly divergences = computed<CustodyDivergence[]>(
    () => this.diagnosis.value()?.divergences ?? [],
  );
  protected readonly reconcileOnly = computed(() => {
    const plan = this.diagnosis.value()?.resolution_plan ?? [];
    return plan.length > 0 && plan.every((step) => !step.mutates);
  });

  // A fresh resolve attempt must never carry over a prior attempt's stale
  // error or receipt — both are part of the audited flow and would otherwise
  // render before any new submission happens.
  protected requestResolve(): void {
    this.resolveError.set(null);
    this.receipt.set(null);
    this.confirmOpen.set(true);
  }

  protected cancelResolve(): void {
    this.resolveError.set(null);
    this.confirmOpen.set(false);
  }

  protected async onConfirmed({ reason }: { reason: string }): Promise<void> {
    const d = this.diagnosis.value();
    if (d === undefined || this.busy()) return;

    this.busy.set(true);
    this.resolveError.set(null);
    try {
      const receipt = await this.brokers.resolveCustody('alpaca', {
        reason,
        snapshot_version: d.snapshot_version,
        confirmation_token: 'RESOLVE',
        idempotency_key: this.newIdempotencyKey(),
      });
      this.receipt.set(this.toReceiptView(receipt));
      this.confirmOpen.set(false);
      this.diagnosis.reload();
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 409) {
        // Never auto-resubmit on a stale snapshot / blocked prerequisite —
        // re-diagnose and let the operator re-confirm against fresh state.
        this.resolveError.set(STATE_CHANGED_MESSAGE);
        this.diagnosis.reload();
        return;
      }
      this.resolveError.set(this.extractErrorMessage(error));
    } finally {
      this.busy.set(false);
    }
  }

  protected dismissReceipt(): void {
    this.receipt.set(null);
  }

  private toReceiptView(receipt: CustodyResolutionReceipt): ActionReceiptView {
    const inSync = receipt.in_sync ?? false;
    const steps = receipt.steps_executed ?? [];
    const remaining = receipt.remaining_divergences ?? [];
    const message = receipt.resolved
      ? steps.length > 0
        ? steps.map((step) => step.message).join(' ')
        : 'Resolution steps completed.'
      : `Cannot fully sync — ${remaining.length} divergence(s) remain.`;
    return {
      actionId: 'resolve',
      outcome: receipt.resolved && inSync ? 'success' : 'failure',
      receiptId: receipt.receipt_id,
      recordedAtMs: receipt.recorded_at_ms,
      message,
      remediation: null,
    };
  }

  private extractErrorMessage(error: unknown): string {
    const detail =
      error instanceof HttpErrorResponse &&
      typeof error.error === 'object' &&
      error.error !== null &&
      'detail' in error.error &&
      typeof error.error.detail === 'object' &&
      error.error.detail !== null
        ? (error.error.detail as Record<string, unknown>)
        : null;
    const message = detail?.['message'];
    if (typeof message === 'string') return message;
    return 'Resolve failed. Try again.';
  }

  private newIdempotencyKey(): string {
    if (typeof globalThis.crypto?.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
    return `custody-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}
