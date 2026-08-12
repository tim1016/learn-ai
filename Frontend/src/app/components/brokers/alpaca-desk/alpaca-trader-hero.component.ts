import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type {
  BrokerAccountSnapshot,
  BrokerActivity,
  BrokerPosition,
} from '../../../api/alpaca.types';

/**
 * Read-only, presentational summary of the selected day's live account facts.
 * P&L remains intentionally unavailable until the canonical account
 * attribution read model is delivered; this component never derives it.
 */
@Component({
  selector: 'app-alpaca-trader-hero',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe],
  templateUrl: './alpaca-trader-hero.component.html',
  styleUrl: './alpaca-trader-hero.component.scss',
})
export class AlpacaTraderHeroComponent {
  readonly account = input<BrokerAccountSnapshot | undefined>(undefined);
  readonly positions = input<readonly BrokerPosition[] | undefined>(undefined);
  readonly positionsUnavailable = input(false);
  readonly activities = input<readonly BrokerActivity[] | undefined>(undefined);
  readonly activitiesUnavailable = input(false);

  protected readonly fillCount = computed(() =>
    this.activities()?.filter((activity) => activity.activity_type === 'FILL').length,
  );
}
