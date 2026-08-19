import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, input, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import type {
  PresentedOperatorAction,
  PresentedOperatorActionRejection,
  PresentedOperatorActionResult,
} from '../../../api/broker-models';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokerService } from '../../../services/broker.service';
import { AccountSafetySnapshotStore } from './account-safety-snapshot.store';

/** Executes only the closed action identifiers explicitly presented by Account Safety. */
@Component({
  selector: 'app-presented-account-action',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, ReceiptLabelPipe],
  templateUrl: './presented-account-action.component.html',
  styleUrl: './presented-account-action.component.scss',
})
export class PresentedAccountActionComponent {
  private readonly broker = inject(BrokerService);
  private readonly accountSafety = inject(AccountSafetySnapshotStore);

  readonly action = input.required<PresentedOperatorAction>();
  readonly busy = signal(false);
  readonly result = signal<PresentedOperatorActionResult | null>(null);
  readonly rejection = signal<PresentedOperatorActionRejection | null>(null);
  readonly transportFailed = signal(false);
  readonly refreshFailed = signal(false);

  async execute(): Promise<void> {
    const action = this.action();
    if (this.busy() || action.availability !== 'AVAILABLE') return;

    this.busy.set(true);
    this.result.set(null);
    this.rejection.set(null);
    this.transportFailed.set(false);
    this.refreshFailed.set(false);
    try {
      const result = await this.dispatch(action);
      this.result.set(result);
    } catch (error: unknown) {
      const rejection = presentedActionRejection(error);
      if (rejection === null) {
        this.transportFailed.set(true);
      } else {
        this.rejection.set(rejection);
      }
    } finally {
      try {
        await this.accountSafety.refresh(action.target.account_id);
      } catch {
        this.refreshFailed.set(true);
      } finally {
        this.busy.set(false);
      }
    }
  }

  private dispatch(action: PresentedOperatorAction): Promise<PresentedOperatorActionResult> {
    switch (action.action_id) {
      case 'reconcile_now':
        return this.broker.executePresentedReconcileNow(action.target.account_id, action);
      default:
        throw new Error('This action is not dispatched from the account-safety panel.');
    }
  }
}

function presentedActionRejection(error: unknown): PresentedOperatorActionRejection | null {
  if (!(error instanceof HttpErrorResponse) || error.status !== 409) return null;
  const detail = error.error?.detail;
  if (
    typeof detail !== 'object'
    || detail === null
    || typeof detail.reason_code !== 'string'
    || typeof detail.message !== 'string'
  ) {
    return null;
  }
  return detail as PresentedOperatorActionRejection;
}
