import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

import type { RecentDecisionView } from '../../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../../shared/timestamp/timestamp-display.component';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';

/**
 * Dry Run's decision evidence for the Trader lens (issue #1729 AC #8).
 *
 * Dry Run places no broker orders, so the trades-today fills rail has
 * nothing to show; this is the Trader-visible record of what the strategy
 * decided instead. Every row here is `simulated`, and `authority_kind`
 * renders through the shared `receiptLabel` pipe so a synthetic (Dry Run)
 * decision stays visibly distinguishable from a real Paper one rather than
 * being an invisible field on the wire contract.
 */
@Component({
  selector: 'app-recent-decisions-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './recent-decisions-list.component.html',
  styleUrl: './recent-decisions-list.component.scss',
})
export class RecentDecisionsListComponent {
  readonly decisions = input<readonly RecentDecisionView[]>([]);

  protected readonly hasDecisions = computed(() => this.decisions().length > 0);

  protected trackDecision(_index: number, decision: RecentDecisionView): number {
    return decision.seq;
  }
}
