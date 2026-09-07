import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';

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
}
