import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';

import type { BrokerActivityHealth } from '../../../../../api/live-instances.types';
import { fmtTimestampLocal } from '../../../format';
import { OperatorNoticeComponent } from '../../../../operator-notice/operator-notice.component';
import { AssetIdentityComponent } from '../../../../../shared/asset-identity';

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
  imports: [AssetIdentityComponent, OperatorNoticeComponent],
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
  readonly selectedCategoryId = signal<string | null>(null);
  readonly expandedRowId = signal<string | null>(null);

  readonly eventCount = computed<number>(() => this.eventRows()?.length ?? this.rows().length);
  readonly hasEventSummary = computed<boolean>(() => this.eventSummary().length > 0);
  readonly selectedSummary = computed<ActivityBrokerCategorySummary | null>(() => {
    const summaries = this.eventSummary();
    if (!summaries.length) return null;
    const selected = this.selectedCategoryId();
    return summaries.find((summary) => summary.category_id === selected) ?? summaries[0];
  });
  readonly selectedRows = computed<ActivityBrokerEventRow[]>(() => {
    const summary = this.selectedSummary();
    const rows = this.eventRows() ?? [];
    if (summary === null) return rows;
    const byId = new Map(rows.map((row) => [this.rowIdentity(row), row]));
    return summary.row_ids
      .map((id) => byId.get(id))
      .filter((row): row is ActivityBrokerEventRow => row !== undefined);
  });

  /** Render-only formatting wrappers used by the template. */
  readonly fmtTimestampLocal = fmtTimestampLocal;

  selectCategory(summary: ActivityBrokerCategorySummary): void {
    this.selectedCategoryId.set(summary.category_id);
    this.expandedRowId.set(null);
  }

  toggleRow(row: ActivityBrokerEventRow): void {
    const id = this.rowIdentity(row);
    this.expandedRowId.update((current) => current === id ? null : id);
  }

  isSelected(summary: ActivityBrokerCategorySummary): boolean {
    return this.selectedSummary()?.category_id === summary.category_id;
  }

  isExpanded(row: ActivityBrokerEventRow): boolean {
    return this.expandedRowId() === this.rowIdentity(row);
  }

  rowIdentity(row: ActivityBrokerEventRow): string {
    return row.visible_row_id || row.id;
  }
}
