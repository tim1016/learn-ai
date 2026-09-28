import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import type { ResourceTarget } from '../../../fleet/resource-target';
import { BrokersService } from '../../../services/brokers.service';

/** Render the custody authority's totals; the browser does not apportion fees. */
@Component({
  selector: 'app-fee-attribution',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe],
  templateUrl: './fee-attribution.component.html',
  styleUrl: './fee-attribution.component.scss',
})
export class FeeAttributionComponent {
  private readonly brokers = inject(BrokersService);
  readonly target = input<ResourceTarget | null>(null);
  readonly strategyInstanceId = input<string | null>(null);
  protected readonly fees = resource({
    params: () => {
      const target = this.target();
      return target === null ? undefined : { target, sid: this.strategyInstanceId() };
    },
    loader: ({ params }) => this.brokers.getFeeAttribution(params.target, params.sid),
  });
  protected readonly view = computed(() => this.fees.hasValue() ? this.fees.value() : null);
  protected readonly rows = computed(() => {
    const sid = this.strategyInstanceId();
    return (this.view()?.rows ?? []).filter(row => sid === null || row.strategy_instance_id === sid);
  });
}
