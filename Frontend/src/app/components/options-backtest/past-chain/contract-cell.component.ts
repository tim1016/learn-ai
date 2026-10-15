import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { ChainContract } from './past-chain.types';
import { cellOf } from './past-chain-view';

/**
 * One contract at one strike: the price of its last trade by the instant and the minute that price is the close of,
 * side by side, or, in words, why it has none. A price is never drawn without its time: an old trade read as a current
 * price is the fault the Past chain exists to prevent. A state this page does not know is said as such. Nothing here is
 * ever blank or a zero.
 */
@Component({
  selector: 'app-options-backtest-chain-contract',
  imports: [AssetIdentityComponent, AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './contract-cell.component.html',
  styleUrl: './contract-cell.component.scss',
})
export class ContractCellComponent {
  readonly contract = input.required<ChainContract>();
  protected readonly cell = computed(() => cellOf(this.contract()));
}
