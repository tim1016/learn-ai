import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { ActivityPeriod, PeriodFees } from '../../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';

/** What each period's fees cover, said once. */
const PERIOD_SCOPE: Readonly<Record<ActivityPeriod, string>> = {
  today: 'today',
  '30d': 'the last 30 trading days',
  '60d': 'the last 60 trading days',
};

/**
 * Fees per bot for one Activity period, one row per owner. Each row is named
 * by the data plane — the bot's own name, or an outside order with its order
 * number — and every amount is its string; the table only lays them out.
 */
@Component({
  selector: 'app-alpaca-activity-fees',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, TimestampDisplayComponent],
  templateUrl: './alpaca-activity-fees.component.html',
  styleUrl: './alpaca-activity.scss',
})
export class AlpacaActivityFeesComponent {
  readonly period = input.required<ActivityPeriod>();
  readonly view = input<PeriodFees | null>(null);
  readonly loading = input(false);
  readonly failed = input(false);
  readonly retry = output();

  protected readonly scope = computed(() => PERIOD_SCOPE[this.period()]);
  /** Why there is no table: the read failed, or the data plane answered that
   * the account's fee record is offline and said why. */
  protected readonly unavailable = computed(() => {
    if (this.failed()) return 'Fees are unavailable right now.';
    const view = this.view();
    return view === null || view.available ? null : (view.messages[0] ?? 'Fees are unavailable right now.');
  });
}
