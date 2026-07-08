import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

import type { BrokerActivityHealth } from '../../../../../api/live-instances.types';
import { fmtTimestampLocal } from '../../../format';
import { OperatorNoticeComponent } from '../../../../operator-notice/operator-notice.component';

import type {
  ActivityBrokerCategorySummary,
  ActivityBrokerEventRow,
} from '../bot-trade-chart-card/bot-trade-chart-card.types';
import type { BrokerActivityRow } from './broker-activity.types';

/**
 * Broker-tail summary projection. The backend owns the row-level facts and
 * emits category summaries; this component intentionally renders only those
 * live-updating cards so the Activity tab does not become a dense broker log.
 */
@Component({
  selector: 'app-broker-activity-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OperatorNoticeComponent],
  templateUrl: './broker-activity-table.component.html',
  styleUrl: './broker-activity-table.component.scss',
})
export class BrokerActivityTableComponent {
  readonly rows = input.required<BrokerActivityRow[]>();
  readonly eventSummary = input<ActivityBrokerCategorySummary[]>([]);
  readonly eventRows = input<ActivityBrokerEventRow[] | null>(null);
  readonly backfillLoading = input<boolean>(false);
  readonly backfillError = input<string | null>(null);
  readonly sseStatus = input<string>('connecting');
  readonly sseError = input<string | null>(null);
  /** PR 5 — typed broker-activity health from the 4s status poll.
   *  When present, replaces the implicit ``backfillLoading`` spinner with
   *  the server-authored health verdict. Null before the first poll response. */
  readonly activityHealth = input<BrokerActivityHealth | null>(null);

  readonly eventCount = computed<number>(() => this.eventRows()?.length ?? this.rows().length);
  readonly hasEventSummary = computed<boolean>(() => this.eventSummary().length > 0);

  /** Render-only formatting wrappers used by the template. */
  readonly fmtTimestampLocal = fmtTimestampLocal;
}
