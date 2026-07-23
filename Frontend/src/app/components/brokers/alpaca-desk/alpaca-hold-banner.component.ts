import { ChangeDetectionStrategy, Component, inject, resource, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokersService } from '../../../services/brokers.service';

/**
 * Alpaca exposure-hold banner (phase-2 S6). Renders ONLY when the clerk reports
 * an active account-level exposure hold: the code-like `reason_code` (rendered
 * through the shared `receiptLabel` pipe) plus the backend-authored `reason`
 * prose (unpiped), and a clear-hold action the operator uses to lift it.
 *
 * The hold is a safety posture — while active, new submits are refused (409) but
 * cancels stay allowed. Clearing it re-queries the status so the banner
 * disappears once the backend confirms the hold is gone. When there is no hold
 * (or the status is still loading / errored) the banner renders nothing.
 */
@Component({
  selector: 'app-alpaca-hold-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, MessageModule, ReceiptLabelPipe],
  templateUrl: './alpaca-hold-banner.component.html',
  host: { class: 'block' },
})
export class AlpacaHoldBannerComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly status = resource({
    loader: () => this.brokers.getClerkStatus(),
  });

  protected readonly clearing = signal(false);
  protected readonly clearError = signal<string | null>(null);

  protected async clearHold(): Promise<void> {
    if (this.clearing()) return;
    this.clearing.set(true);
    this.clearError.set(null);
    try {
      await this.brokers.clearHold('alpaca', {
        reason: 'Operator cleared the exposure hold from the Alpaca desk.',
      });
      // Re-query so the banner reflects the backend's post-clear truth rather
      // than assuming success optimistically.
      this.status.reload();
    } catch {
      this.clearError.set('Could not clear the hold. Please try again.');
    } finally {
      this.clearing.set(false);
    }
  }
}
