import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { durationRangeText, EXPOSURE_CLAIMS, EXPOSURE_LABELS } from './golden-search-display';
import type { GoldenSearchMethod, GoldenSearchPreflight } from './golden-search.types';

/**
 * The server's answer to the current plan (#2696): every refusal, the
 * per-stage workload bound against the cap with the reserved final-test and
 * proof work, the serial-time range (labelled an estimate), the fold plan,
 * the warmup run-up and the exposure state of the final-test dates.
 */
@Component({
  selector: 'app-golden-search-preflight-panel',
  imports: [DecimalPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-preflight-panel.component.html',
  styleUrl: './golden-search-preflight-panel.component.scss',
})
export class GoldenSearchPreflightPanelComponent {
  readonly preflight = input<GoldenSearchPreflight | null>(null);
  readonly checking = input(false);
  /** Why the plan could not be checked (a failed request). */
  readonly error = input<string | null>(null);
  /** Why the plan is not being checked yet (unreadable inputs, dates still being laid out). */
  readonly blocked = input<string | null>(null);
  readonly method = input<GoldenSearchMethod>('zoom');

  protected readonly exposureLabels = EXPOSURE_LABELS;
  protected readonly exposureClaims = EXPOSURE_CLAIMS;
  protected readonly duration = computed(() => {
    const estimate = this.preflight()?.estimate;
    return estimate ? durationRangeText(estimate.serial_seconds_low, estimate.serial_seconds_high) : null;
  });
}
