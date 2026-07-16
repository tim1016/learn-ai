import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Router } from '@angular/router';

import type { AccountRosterRow } from '../../../api/account-directory.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { AccountDeskDirectoryStore } from '../account-desk/account-desk-directory-store.service';

/** Entry roster for the account-keyed desk. */
@Component({
  selector: 'app-account-roster-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-roster-page.component.html',
  styleUrl: './account-roster-page.component.scss',
})
export class AccountRosterPageComponent {
  readonly directory = inject(AccountDeskDirectoryStore);
  private readonly router = inject(Router);

  constructor() {
    void this.directory.loadRoster();
  }

  openDesk(row: AccountRosterRow): void {
    void this.router.navigate(['/broker/accounts', row.account_id]);
  }

  retry(): void {
    this.directory.retryRoster();
  }
}
