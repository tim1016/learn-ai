import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { UnderlyingPrice } from './past-chain.types';
import { underlyingViewOf } from './past-chain-view';

/**
 * The underlying's own price at the instant a chain is read at, with the minute that price is the close of, or in
 * words why there is none. A price is never drawn without its time, and a state this page does not know is said as such.
 */
@Component({
  selector: 'app-options-backtest-underlying-strip',
  imports: [AssetIdentityComponent, AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './underlying-strip.component.html',
  styleUrl: './underlying-strip.component.scss',
})
export class UnderlyingStripComponent {
  readonly symbol = input.required<string>();
  readonly price = input.required<UnderlyingPrice>();

  protected readonly view = computed(() => underlyingViewOf(this.price()));
}
