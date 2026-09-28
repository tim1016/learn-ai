import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { ActivityPeriodStatement } from '../../../../api/alpaca.types';

/**
 * Today's money statement: realized, fees, open and net, each amount and the
 * reason for any missing one authored by the data plane. It formats the
 * strings it is given and adds nothing up — an amount the backend could not
 * know reads "Unavailable", never $0.00.
 */
@Component({
  selector: 'app-alpaca-activity-statement',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe],
  templateUrl: './alpaca-activity-statement.component.html',
  styleUrl: './alpaca-activity.scss',
})
export class AlpacaActivityStatementComponent {
  readonly statement = input<ActivityPeriodStatement | null>(null);
  readonly loading = input(false);
  readonly failed = input(false);
  /** Re-read the period; the statement arrives with the period's fees. */
  readonly retry = output();
}
