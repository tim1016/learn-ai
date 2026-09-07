import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

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
    .alpaca-banner.is-paper { background: var(--info-soft); color: var(--info); border-color: rgba(41, 182, 246, 0.42); }
    .alpaca-banner.is-live-unarmed { background: var(--warn-soft, #fff3e0); color: var(--warn, #b45309); border-color: currentColor; font-weight: 600; }
    .alpaca-banner.is-live-armed { background: var(--danger-soft, #fde8e8); color: var(--danger, #b91c1c); border-color: currentColor; font-weight: 700; }
    .alpaca-banner.is-unknown { background: var(--surface-sunken, transparent); color: var(--text-secondary); border-style: dashed; }
    .alpaca-banner__kicker { font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; font-size: 0.68rem; }
    .alpaca-banner__armed { opacity: 0.85; }
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
        [attr.aria-label]="ariaLabel()"
        [attr.title]="v.detail"
      >
        <span class="alpaca-banner__kicker">Alpaca</span>
        <span>{{ v.headline }}</span>
        @if (v.configured_mode === 'live') {
          <span class="alpaca-banner__armed">· {{ v.armed_instance_count }} armed</span>
        }
        @if (v.clerk_refusal_reason_code && v.final_verdict === 'unknown') {
          <span>· {{ v.clerk_refusal_reason_code | receiptLabel }}</span>
        }
      </div>
    }
  `,
})
export class AlpacaLiveBannerComponent {
  private readonly service = inject(AlpacaLiveVerdictService);

  protected readonly verdict = this.service.verdict;

  protected readonly ariaLabel = computed(() => {
    const v = this.verdict();
    if (!v) return '';
    const money = v.configured_mode === 'live' ? 'real money' : 'no real money at risk';
    return `Alpaca account mode: ${v.final_verdict}, ${money}. ${v.headline}`;
  });
}
