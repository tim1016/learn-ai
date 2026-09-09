import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { AlpacaLiveVerdictService } from '../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';

/**
 * Global Alpaca account-mode banner (ADR 0059 D8; the ADR 0011 trust anchor
 * for the Alpaca path). Renders the server verdict and nothing it derives
 * itself: the headline and detail are backend-authored, the mode class is
 * the verdict's own ``final_verdict``. On a live account the account id,
 * the mode and the armed count are always on screen.
 */
@Component({
  selector: 'app-alpaca-live-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  styles: [`
    :host { display: contents; }
    .alpaca-banner {
      display: flex; align-items: center; gap: 0.6rem;
      padding: 0.25rem 0.75rem; border-radius: var(--radius-pill);
      font-size: 0.8rem; line-height: 1.2; white-space: nowrap;
      border: 1px solid var(--border-strong); color: var(--text-secondary);
    }
    .alpaca-banner.is-paper { background: var(--info-soft); color: var(--info); border-color: var(--info); }
    .alpaca-banner.is-live-unarmed { background: var(--warn-soft); color: var(--warn); border-color: currentColor; font-weight: 600; }
    .alpaca-banner.is-live-armed { background: var(--bear-soft); color: var(--bear); border-color: currentColor; font-weight: 700; }
    .alpaca-banner.is-unknown { background: var(--bg-sunken); color: var(--text-secondary); border-style: dashed; }
    .alpaca-banner__kicker { font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; font-size: 0.68rem; }
    .alpaca-banner__armed { opacity: 0.85; }
    .alpaca-banner__hold { font-weight: 700; color: var(--bear); }
  `],
  template: `
    @let v = verdict();
    @if (v) {
      <div
        class="alpaca-banner"
        [class.is-paper]="v.final_verdict === 'paper'"
        [class.is-live-unarmed]="v.final_verdict === 'live-unarmed'"
        [class.is-live-armed]="v.final_verdict === 'live-armed'"
        [class.is-unknown]="v.final_verdict === 'unknown'"
        role="status"
        [attr.title]="v.detail"
      >
        <span class="alpaca-banner__kicker">Alpaca</span>
        <span>{{ v.headline }}</span>
        @if (v.configured_mode === 'live') {
          <span class="alpaca-banner__armed">· {{ v.armed_instance_count }} armed</span>
        }
        @if (v.loss_hold === 'held') {
          <span class="alpaca-banner__hold">· loss hold</span>
        }
        @if (v.clerk_refusal_reason_code && v.final_verdict === 'unknown') {
          <span>· {{ v.clerk_refusal_reason_code | receiptLabel }}</span>
        }
      </div>
    } @else if (lastError()) {
      <div class="alpaca-banner is-unknown" role="status" [attr.title]="unavailableDetail">
        <span class="alpaca-banner__kicker">Alpaca</span>
        <span>{{ unavailableHeadline }}</span>
      </div>
    }
  `,
})
export class AlpacaLiveBannerComponent {
  private readonly service = inject(AlpacaLiveVerdictService);

  protected readonly verdict = this.service.verdict;
  protected readonly lastError = this.service.lastError;
  // Closed operator copy for a transport fact the server cannot author — the
  // poll itself failed. The trust anchor degrades to an explicit unknown,
  // never to silence (ADR 0011 §2).
  protected readonly unavailableHeadline = 'Account verdict unavailable — the last read failed';
  protected readonly unavailableDetail =
    'The shell could not read the Alpaca live verdict. No mode is assumed until a read succeeds.';
}
