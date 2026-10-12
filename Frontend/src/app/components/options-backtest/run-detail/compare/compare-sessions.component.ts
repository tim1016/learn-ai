import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { RunComparison } from '../run-detail.types';

/**
 * The sessions both runs tested: how many there are and how many each run has that the other lacks, each run's net profit,
 * trades and winning sessions over the shared sessions only, and the pair of each shared session's profits. The server
 * worked out every figure; this subtracts nothing, so there is no column of differences.
 */
@Component({
  selector: 'app-options-backtest-compare-sessions',
  imports: [AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './compare-sessions.component.html',
  styleUrl: './compare-tables.scss',
})
export class CompareSessionsComponent {
  readonly comparison = input.required<RunComparison>();

  protected readonly overlap = computed(() => this.comparison().overlap);
}
