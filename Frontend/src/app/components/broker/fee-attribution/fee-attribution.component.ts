import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import type { ActivityPeriod } from '../../../api/alpaca.types';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { BrokersService } from '../../../services/brokers.service';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

let nextFeeHeadingId = 0;

/** What an Activity period's fees cover, said once. */
const PERIOD_SCOPE: Readonly<Record<ActivityPeriod, string>> = {
  today: 'Today',
  '30d': 'Last 30 trading days',
  '60d': 'Last 60 trading days',
};

/**
 * Render the custody authority's fee totals, one block per owner; the
 * browser does not apportion fees. A bot's page reads that bot's lifetime;
 * the account's Activity page reads one period's fees for every owner.
 */
@Component({
  selector: 'app-fee-attribution',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, TimestampDisplayComponent],
  templateUrl: './fee-attribution.component.html',
  styleUrl: './fee-attribution.component.scss',
})
export class FeeAttributionComponent {
  private readonly brokers = inject(BrokersService);
  readonly target = input<ResourceTarget | null>(null);
  readonly strategyInstanceId = input<string | null>(null);
  /** An Activity period of the account's fees, or `null` for the lifetime. */
  readonly period = input<ActivityPeriod | null>(null);
  /** The heading's level in its host page's outline. */
  readonly headingLevel = input<2 | 3>(3);

  protected readonly headingId = `fee-attribution-heading-${nextFeeHeadingId++}`;
  protected readonly fees = resource({
    params: () => {
      const target = this.target();
      return target === null ? undefined : { target, sid: this.strategyInstanceId(), period: this.period() };
    },
    loader: ({ params }) => this.brokers.getFeeAttribution(params.target, params.sid, params.period),
  });
  protected readonly view = computed(() => this.fees.hasValue() ? this.fees.value() : null);
  protected readonly rows = computed(() => {
    const sid = this.strategyInstanceId();
    return (this.view()?.rows ?? []).filter(row => sid === null || row.strategy_instance_id === sid);
  });
  protected readonly scope = computed(() => {
    const period = this.period();
    return period === null ? null : PERIOD_SCOPE[period];
  });
}
