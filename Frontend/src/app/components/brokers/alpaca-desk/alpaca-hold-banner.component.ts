import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  resource,
  signal,
} from '@angular/core';
import { MessageModule } from 'primeng/message';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokersService } from '../../../services/brokers.service';

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
 */
@Component({
  selector: 'app-alpaca-hold-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MessageModule, ReceiptLabelPipe],
  templateUrl: './alpaca-hold-banner.component.html',
  host: { class: 'block' },
})
export class AlpacaHoldBannerComponent {
  private readonly brokers = inject(BrokersService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly refreshEpoch = signal(0);

  protected readonly status = resource({
    params: () => this.refreshEpoch(),
    loader: () => this.brokers.getClerkStatus(),
  });

  constructor() {
    // Holds can be raised by the background reconciliation sweep while this
    // desk remains open. Polling gives the operator that asynchronous safety
    // state without requiring a route reload or a separate streaming channel.
    const refreshTimer = setInterval(() => {
      this.refreshEpoch.update((epoch) => epoch + 1);
    }, 15_000);
    this.destroyRef.onDestroy(() => clearInterval(refreshTimer));
  }
}
