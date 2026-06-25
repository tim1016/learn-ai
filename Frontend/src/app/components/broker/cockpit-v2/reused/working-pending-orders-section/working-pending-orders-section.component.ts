import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { fmtNumber, fmtTimestampNy } from '../../../format';

import type { ActivityOrderRow } from '../bot-trade-chart-card/bot-trade-chart-card.types';

type OrderGroup = 'active' | 'engine_pending' | 'resolved';

interface OrderDisplay {
  row: ActivityOrderRow;
  submittedTs: string;
  updatedTs: string;
}

/**
 * Orders Today — same-day broker order blotter rendered from the
 * backend-materialized Activity projection. Active, engine-pending, and
 * resolved orders are separated by server-authored group values.
 *
 * Render-only. The frontend formats numbers/timestamps and groups rows
 * by the backend-provided group; it does not infer order lifecycle state.
 */
@Component({
  selector: 'app-working-pending-orders-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  templateUrl: './working-pending-orders-section.component.html',
  styleUrl: './working-pending-orders-section.component.scss',
})
export class WorkingPendingOrdersSectionComponent {
  readonly orders = input.required<ActivityOrderRow[]>();

  readonly displayRows = computed<OrderDisplay[]>(() =>
    this.orders()
      .map((row) => ({
        row,
        submittedTs: fmtTimestampNy(row.submitted_ts_ms),
        updatedTs: fmtTimestampNy(row.last_update_ts_ms),
      }))
      .sort((a, b) => b.row.last_update_ts_ms - a.row.last_update_ts_ms),
  );

  readonly hasOrders = computed<boolean>(() => this.displayRows().length > 0);
  readonly activeRows = computed<OrderDisplay[]>(() => this.rowsFor('active'));
  readonly enginePendingRows = computed<OrderDisplay[]>(() => this.rowsFor('engine_pending'));
  readonly resolvedRows = computed<OrderDisplay[]>(() => this.rowsFor('resolved'));

  readonly fmtNumber = fmtNumber;

  trackRow = (_i: number, p: OrderDisplay): string => p.row.order_key;

  private rowsFor(group: OrderGroup): OrderDisplay[] {
    return this.displayRows().filter((p) => p.row.group === group);
  }
}
