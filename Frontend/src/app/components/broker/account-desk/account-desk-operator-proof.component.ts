import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { AccountDeskGuidanceComponent } from './account-desk-guidance.component';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

/** Reconciliation and observation evidence projected without browser-side verdict derivation. */
@Component({
  selector: 'app-account-desk-operator-proof',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AccountDeskGuidanceComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-desk-operator-proof.component.html',
  styleUrl: './account-desk-operator-proof.component.scss',
})
export class AccountDeskOperatorProofComponent {
  readonly store = inject(AccountDeskSurfaceStore);

  retry(): void {
    this.store.retry();
  }
}
