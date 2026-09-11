import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { AlpacaLiveVerdictService } from '../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';

/**
 * Compact Alpaca account-mode trust anchor (ADR 0059 D8; ADR 0011).
 * The server verdict remains the only truth source. Live mode keeps the
 * account id and armed count visible even in the dense global header.
 */
@Component({
  selector: 'app-alpaca-live-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  styles: [`
    :host { display: contents; }
    .alpaca-banner {
      display: inline-flex; min-height: 30px; align-items: center; gap: 0.35rem;
      padding: 0 0.65rem; border-radius: var(--radius-pill);
      font-size: var(--fs-xs); line-height: 1.2; white-space: nowrap;
      border: 1px solid rgba(178, 181, 190, 0.45); color: var(--text-primary);
      background: rgba(5, 8, 14, 0.42); font-variant-numeric: tabular-nums;
    }
    .alpaca-banner.is-paper { color: #b9edff; border-color: #45b9e1; }
    .alpaca-banner.is-live-unarmed { color: #ffd0cf; border-color: #f06b68; font-weight: 650; }
    .alpaca-banner.is-live-armed { color: #fff; border-color: #ff8b88; background: rgba(90, 12, 20, 0.72); font-weight: 750; }
    .alpaca-banner.is-unknown { color: var(--text-secondary); border-style: dashed; }
    .alpaca-banner__mode { font-weight: 750; }
    .alpaca-banner__detail { opacity: 0.86; }
    .alpaca-banner__hold { font-weight: 750; color: #ffaaa8; }
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
        [attr.aria-label]="v.headline"
        [attr.title]="v.detail"
      >
        @if (v.configured_mode === 'paper') {
          <span class="alpaca-banner__mode">Paper money</span>
        } @else if (v.configured_mode === 'live') {
          <span class="alpaca-banner__mode">Live</span>
          <span class="alpaca-banner__detail">· {{ v.observed_account_id ?? 'account unknown' }}</span>
          <span class="alpaca-banner__detail">· {{ v.armed_instance_count }} armed</span>
        } @else {
          <span class="alpaca-banner__mode">Mode unknown</span>
        }
        @if (v.loss_hold === 'held') {
          <span class="alpaca-banner__hold">· loss hold</span>
        }
        @if (v.clerk_refusal_reason_code && v.final_verdict === 'unknown') {
          <span>· {{ v.clerk_refusal_reason_code | receiptLabel }}</span>
        }
      </div>
    } @else if (lastError()) {
      <div
        class="alpaca-banner is-unknown"
        role="status"
        [attr.aria-label]="unavailableHeadline"
        [attr.title]="unavailableDetail"
      >
        <span class="alpaca-banner__mode">Mode unavailable</span>
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
