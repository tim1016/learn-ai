import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { TodayStatement } from '../../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';

/**
 * Today's money statement: realized gains, fees, the change in open gains
 * since the last close, and the net, for the bots and this app's orders.
 * Every amount, and the reason for any missing one, is authored by the data
 * plane; this formats the strings it is given and adds nothing up. An amount
 * the backend could not know reads "Unavailable", never $0.00.
 */
@Component({
  selector: 'app-alpaca-activity-statement',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, TimestampDisplayComponent],
  templateUrl: './alpaca-activity-statement.component.html',
  styleUrl: './alpaca-activity.scss',
})
export class AlpacaActivityStatementComponent {
  readonly statement = input<TodayStatement | null>(null);
  readonly loading = input(false);
  readonly failed = input(false);
  readonly retry = output();

  /** Whether the data plane could state any figure at all; when it could
   * not, its reason is the whole card. */
  protected readonly hasFigures = computed(() => {
    const today = this.statement();
    return today !== null && [today.realized_usd, today.fees_usd, today.open_change_usd, today.net_usd]
      .some((amount) => amount !== null);
  });
}
