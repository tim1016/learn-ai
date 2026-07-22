import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { StaleBindingRetirementCandidate } from '../../../api/account-reconciliation.types';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';

/** Server-proven stale deployment bindings with one exact retirement each. */
@Component({
  selector: 'app-account-desk-stale-binding-cure',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-desk-stale-binding-cure.component.html',
  styleUrl: './account-desk-stale-binding-cure.component.scss',
})
export class AccountDeskStaleBindingCureComponent {
  readonly recovery = inject(AccountDeskRecoveryStore);

  requestRetirement(candidate: StaleBindingRetirementCandidate): void {
    this.recovery.requestStaleBindingRetirement(candidate);
  }
}
