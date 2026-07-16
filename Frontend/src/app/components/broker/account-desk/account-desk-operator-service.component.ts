import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';

/** Read-only operational evidence for the backend-owned Account service. */
@Component({
  selector: 'app-account-desk-operator-service',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-desk-operator-service.component.html',
  styleUrl: './account-desk-operator-service.component.scss',
})
export class AccountDeskOperatorServiceComponent {
  readonly directory = inject(AccountDeskDirectoryStore);

  retry(): void {
    this.directory.retryServiceStatus();
  }
}
