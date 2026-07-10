import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

import type { BrokerActivityHealth } from '../../../../../api/live-instances.types';
import { fmtTimestampLocal } from '../../../format';
import { OperatorNoticeComponent } from '../../../../operator-notice/operator-notice.component';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';

import type { ActivityBrokerCategorySummary } from '../bot-trade-chart-card/bot-trade-chart-card.types';
import type { BrokerActivityRow } from './broker-activity.types';

/**
 * Broker-tail summary projection. The backend owns the row-level facts and
 * emits category summaries; this component intentionally renders only those
 * live-updating cards so the Activity tab does not become a dense broker log.
 */
@Component({
  selector: 'app-broker-activity-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OperatorNoticeComponent, ReceiptLabelPipe],
  templateUrl: './broker-activity-table.component.html',
  styleUrl: './broker-activity-table.component.scss',
})
export class BrokerActivityTableComponent {
  readonly rows = input.required<BrokerActivityRow[]>();
  readonly eventSummary = input<ActivityBrokerCategorySummary[]>([]);
  readonly backfillLoading = input<boolean>(false);
  readonly backfillError = input<string | null>(null);
  readonly sseStatus = input<string>('connecting');
  readonly sseError = input<string | null>(null);
  /** Typed broker-activity health from the state stream.
   *  When present, replaces the implicit ``backfillLoading`` spinner with
   *  the server-authored health verdict. Null before the first poll response. */
  readonly activityHealth = input<BrokerActivityHealth | null>(null);

  readonly eventCount = computed<number>(() => {
    const summaries = this.eventSummary();
    if (summaries.length === 0) return this.rows().length;
    return summaries.reduce((total, summary) => total + summary.event_count, 0);
  });
  readonly hasEventSummary = computed<boolean>(() => this.eventSummary().length > 0);

  /** Render-only formatting wrappers used by the template. */
  readonly fmtTimestampLocal = fmtTimestampLocal;
}
