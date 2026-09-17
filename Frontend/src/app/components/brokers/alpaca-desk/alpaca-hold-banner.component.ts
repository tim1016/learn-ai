import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { MessageModule } from 'primeng/message';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';

/**
 * Alpaca exposure-hold banner (phase-2 S6). Renders ONLY when the clerk reports
 * an active account-level exposure hold: the code-like `reason_code` (rendered
 * through the shared `receiptLabel` pipe) plus the backend-authored `reason`
 * prose (unpiped). Mutating recovery is presented only by the SQLite custody
 * projection, never by a generic direct-clear control.
 *
 * The hold is a safety posture — while active, new submits are refused (409) but
 * cancels stay allowed. When there is no hold (or the status is still loading /
 * errored) the banner renders nothing.
 *
 * Reads `AlpacaDeskAccountDataService.clerkStatus` rather than polling
 * `getClerkStatus` on its own: the account workspace's sync indicator names
 * the same Clerk status, and the two used to read it independently — two 15 s
 * timers against the same endpoint for the same account (#2185). The account
 * workspace's own poll (`AlpacaAccountWorkspaceComponent`) is what reloads
 * the shared resource now, so this banner needs an ancestor that provides
 * `AlpacaDeskAccountDataService` — the account workspace, in production.
 */
@Component({
  selector: 'app-alpaca-hold-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MessageModule, ReceiptLabelPipe],
  templateUrl: './alpaca-hold-banner.component.html',
  host: { class: 'block' },
})
export class AlpacaHoldBannerComponent {
  private readonly accountData = inject(AlpacaDeskAccountDataService);

  protected readonly status = this.accountData.clerkStatus;
}
