import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';

interface MarginRow {
  readonly label: string;
  readonly value: number | null | undefined;
  readonly currency: boolean;
}

/**
 * Read-only margin facts for the Alpaca account card (ADR 0059 D7): shown
 * so an operator can see exposure sits under cash. The trading envelope
 * never reads these fields.
 */
@Component({
  selector: 'app-alpaca-account-margin',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe],
  templateUrl: './alpaca-account-margin.component.html',
  styleUrl: './alpaca-account-margin.component.scss',
})
export class AlpacaAccountMarginComponent {
  readonly account = input.required<BrokerAccountSnapshot>();

  /** The seven margin facts in display order (ADR 0059 D7); absence renders as a dash. */
  protected readonly rows = computed<readonly MarginRow[]>(() => {
    const a = this.account();
    return [
      { label: 'Multiplier', value: a.multiplier, currency: false },
      { label: 'Reg T BP', value: a.regt_buying_power, currency: true },
      { label: 'Day-trading BP', value: a.daytrading_buying_power, currency: true },
      { label: 'Maintenance margin', value: a.maintenance_margin, currency: true },
      { label: 'Initial margin', value: a.initial_margin, currency: true },
      { label: 'SMA', value: a.sma, currency: true },
      { label: 'Last equity', value: a.last_equity, currency: true },
    ];
  });
}
