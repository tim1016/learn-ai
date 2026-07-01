import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { fmtNumber, fmtTimestampLocal } from '../../../format';

import type { ActivityOrderRow } from '../bot-trade-chart-card/bot-trade-chart-card.types';

type OrderGroup = 'active' | 'engine_pending' | 'resolved';

const QUANTITY = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 6,
});

interface OrderDisplay {
  row: ActivityOrderRow;
  chartTs: string;
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
        chartTs: fmtTimestampLocal(row.chart_ts_ms),
      }))
      .sort((a, b) => b.row.chart_ts_ms - a.row.chart_ts_ms),
  );

  readonly hasOrders = computed<boolean>(() => this.displayRows().length > 0);
  readonly activeRows = computed<OrderDisplay[]>(() => this.rowsFor('active'));
  readonly enginePendingRows = computed<OrderDisplay[]>(() => this.rowsFor('engine_pending'));
  readonly resolvedRows = computed<OrderDisplay[]>(() => this.rowsFor('resolved'));

  readonly fmtNumber = fmtNumber;

  trackRow = (_i: number, p: OrderDisplay): string => p.row.order_key;

  orderSummary(row: ActivityOrderRow): string {
    const orderType = row.order_type ? ` ${row.order_type}` : '';
    return `${this.fmtQuantity(row.quantity)}${orderType}`;
  }

  orderDetail(row: ActivityOrderRow): string {
    const parts = [row.status];
    if (row.replay_count > 1) parts.push(`seen ${row.replay_count}x`);
    return parts.filter(Boolean).join(' · ');
  }

  filledSummary(row: ActivityOrderRow): string {
    return `${this.fmtQuantity(row.filled_quantity)} / ${this.fmtQuantity(row.quantity)}`;
  }

  private rowsFor(group: OrderGroup): OrderDisplay[] {
    return this.displayRows().filter((p) => p.row.group === group);
  }

  private fmtQuantity(value: number | null | undefined): string {
    return value == null ? '—' : QUANTITY.format(value);
  }
}
