import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { LegacyStaleClaimCandidate } from '../../../api/account-reconciliation.types';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';

/** Server-proven legacy claim candidates with one exact confirmation each. */
@Component({
  selector: 'app-account-desk-legacy-claim-cure',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-desk-legacy-claim-cure.component.html',
  styleUrl: './account-desk-legacy-claim-cure.component.scss',
})
export class AccountDeskLegacyClaimCureComponent {
  readonly recovery = inject(AccountDeskRecoveryStore);

  requestRetirement(candidate: LegacyStaleClaimCandidate): void {
    this.recovery.requestLegacyRetirement(candidate);
  }
}
