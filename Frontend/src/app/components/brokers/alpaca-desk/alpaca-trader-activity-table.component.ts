import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';
import { Table, TableModule } from 'primeng/table';

import type { BrokerActivity } from '../../../api/alpaca.types';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

function eventTime(activity: BrokerActivity): number {
  return activity.occurred_at_ms ?? activity.observed_at_ms;
}

interface ActivityTableRow {
  readonly id: string;
  readonly timeMs: number;
  readonly observedAtMs: number;
  readonly symbol: string | null;
  readonly activityType: string;
  readonly category: string | null;
  readonly side: string | null;
  readonly quantity: number | null;
  readonly price: number | null;
  readonly netAmount: number | null;
  readonly nativeOrderId: string | null | undefined;
}

/** Compact, client-filtered presentation of broker-authored account activity. */
@Component({
  selector: 'app-alpaca-trader-activity-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    ButtonModule,
    CurrencyPipe,
    DecimalPipe,
    InputText,
    ReceiptLabelPipe,
    TableModule,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-trader-activity-table.component.html',
  styleUrl: './alpaca-trader-activity-table.component.scss',
})
export class AlpacaTraderActivityTableComponent {
  readonly activities = input<readonly BrokerActivity[] | undefined>(undefined);
  readonly unavailable = input(false);

  protected readonly rows = computed<ActivityTableRow[]>(() =>
    [...(this.activities() ?? [])]
      .sort((left, right) => eventTime(right) - eventTime(left))
      .map((activity) => ({
        id: activity.activity_id,
        timeMs: eventTime(activity),
        observedAtMs: activity.observed_at_ms,
        symbol: activity.symbol,
        activityType: activity.activity_type,
        category: activity.category,
        side: activity.side,
        quantity: activity.quantity,
        price: activity.price,
        netAmount: activity.net_amount,
        nativeOrderId: activity.native_order_id,
      })),
  );

  protected readonly globalFilterFields = [
    'symbol',
    'activityType',
    'category',
    'side',
    'quantity',
    'price',
    'netAmount',
    'nativeOrderId',
  ];
  protected readonly tablePassThrough = {
    table: { 'aria-label': "Today's account activity" },
  };

  protected inputValue(event: Event): string {
    return event.target instanceof HTMLInputElement ? event.target.value : '';
  }

  protected clearSearch(table: Table, input: HTMLInputElement): void {
    input.value = '';
    table.clear();
  }
}
