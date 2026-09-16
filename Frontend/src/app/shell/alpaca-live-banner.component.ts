import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import type { LaneDescriptor } from '../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService } from '../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';

/**
 * Compact Alpaca account-mode trust anchor for ONE lane (ADR 0059 D8;
 * ADR 0011; #2110). Renders exactly the lane it is given — the shell mounts
 * one instance per `FleetDirectoryService.lanesOf('alpaca')` entry, never a
 * merged or hardcoded pair, so every badge carries its own lane label.
 *
 * The server verdict remains the only truth source. Live mode keeps the
 * account id and armed count visible even in the dense global header.
 *
 * A lane whose mode cannot be determined — the server itself reports
 * `final_verdict: 'unknown'`, or this lane's own read failed — renders a
 * LOUD warning, never the grey "not configured" styling: an undeterminable
 * lane could be real money, so the badge says so in its own text, not only
 * through colour (WCAG 1.4.1).
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
    .alpaca-banner.is-undetermined { color: #fff; border-color: #ffb020; background: rgba(122, 61, 0, 0.85); font-weight: 750; }
    .alpaca-banner__lane { font-weight: 750; opacity: 0.8; }
    .alpaca-banner__mode { font-weight: 750; }
    .alpaca-banner__detail { opacity: 0.86; }
    .alpaca-banner__hold { font-weight: 750; color: #ffaaa8; }
  `],
  template: `
    @let s = state();
    @let v = s.verdict;
    @if (v) {
      <div
        class="alpaca-banner"
        [class.is-paper]="v.final_verdict === 'paper'"
        [class.is-live-unarmed]="v.final_verdict === 'live-unarmed'"
        [class.is-live-armed]="v.final_verdict === 'live-armed'"
        [class.is-undetermined]="v.final_verdict === 'unknown'"
        role="status"
        [attr.aria-label]="laneLabel() + ': ' + v.headline"
        [attr.title]="v.detail"
      >
        <span class="alpaca-banner__lane">{{ laneLabel() }}</span>
        @if (v.final_verdict === 'paper') {
          <span class="alpaca-banner__mode">Paper money</span>
        } @else if (v.final_verdict === 'live-unarmed' || v.final_verdict === 'live-armed') {
          <span class="alpaca-banner__mode">Live</span>
          <span class="alpaca-banner__detail">· {{ v.observed_account_id ?? 'account unknown' }}</span>
          <span class="alpaca-banner__detail">· {{ v.armed_instance_count }} armed</span>
        } @else {
          <span class="alpaca-banner__mode">Mode unknown — assume real money</span>
        }
        @if (v.loss_hold === 'held') {
          <span class="alpaca-banner__hold">· loss hold</span>
        }
        @if (v.clerk_refusal_reason_code && v.final_verdict === 'unknown') {
          <span>· {{ v.clerk_refusal_reason_code | receiptLabel }}</span>
        }
      </div>
    } @else if (s.lastError) {
      <div
        class="alpaca-banner is-undetermined"
        role="status"
        [attr.aria-label]="laneLabel() + ': ' + unavailableHeadline"
        [attr.title]="unavailableDetail"
      >
        <span class="alpaca-banner__lane">{{ laneLabel() }}</span>
        <span class="alpaca-banner__mode">Mode unavailable — assume real money</span>
      </div>
    }
  `,
})
export class AlpacaLiveBannerComponent {
  private readonly service = inject(AlpacaLiveVerdictService);

  readonly lane = input.required<LaneDescriptor>();

  protected readonly laneLabel = computed(() => this.lane().display_label);
  protected readonly state = computed(() => this.service.stateFor(this.lane().clerk_id));

  // Closed operator copy for a transport fact the server cannot author — the
  // poll itself failed. The trust anchor degrades to an explicit warning,
  // never to silence and never to a reassuring grey (ADR 0011 §2; #2110 D2).
  protected readonly unavailableHeadline = 'Account verdict unavailable — the last read failed';
  protected readonly unavailableDetail =
    "The shell could not read this lane's Alpaca live verdict. Assume real money until a read succeeds.";
}
