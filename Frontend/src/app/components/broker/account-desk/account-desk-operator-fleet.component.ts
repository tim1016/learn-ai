import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { AccountDeskFleetStore } from './account-desk-fleet-store.service';

/** Read-only fleet contamination evidence, as authored by the fleet service. */
@Component({
  selector: 'app-account-desk-operator-fleet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-desk-operator-fleet.component.html',
  styleUrl: './account-desk-operator-fleet.component.scss',
})
export class AccountDeskOperatorFleetComponent {
  readonly store = inject(AccountDeskFleetStore);
  readonly netPositionRows = computed(() => Object.entries(this.store.summary()?.contamination.net_positions ?? {}));

  retry(): void {
    this.store.retry();
  }
}
