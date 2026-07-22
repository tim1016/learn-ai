import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Router } from '@angular/router';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import {
  OperatorBlockerListComponent,
  type OperatorBlockerMoveEvent,
} from '../shared/operator-blocker-list/operator-blocker-list.component';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';

/** Account-scoped Clerk and daemon posture with only backend-declared recovery moves. */
@Component({
  selector: 'app-account-desk-cockpit-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OperatorBlockerListComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-desk-cockpit-status.component.html',
  styleUrl: './account-desk-cockpit-status.component.scss',
})
export class AccountDeskCockpitStatusComponent {
  private readonly router = inject(Router);
  readonly directory = inject(AccountDeskDirectoryStore);
  private readonly recovery = inject(AccountDeskRecoveryStore);

  retry(): void {
    this.directory.retryServiceStatus();
  }

  followMove(event: OperatorBlockerMoveEvent): void {
    if (event.move.action.kind === 'confirm_in_form') {
      this.recovery.requestCockpitMove(event);
      return;
    }
    if (event.move.action.kind === 'navigate') {
      void this.router.navigate([event.move.action.route], {
        fragment: event.move.action.fragment ?? undefined,
      });
    }
  }
}
