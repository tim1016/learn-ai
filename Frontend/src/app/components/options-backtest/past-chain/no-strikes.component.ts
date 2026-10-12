import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { NoStrikesWhy } from './past-chain-view';

/**
 * Why a chain draws no strikes although contracts are listed: the server centres the strikes on the underlying's price,
 * so when the underlying has none at that time there are none to draw, and the underlying's own state says why. This
 * is missing data, never a missing contract, and the listed counts are said as served beside it. It offers what helps:
 * a later time when a bar may have closed by then, the download when the bars are not stored.
 */
@Component({
  selector: 'app-options-backtest-no-strikes',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './no-strikes.component.html',
  styleUrl: './past-chain.scss',
})
export class NoStrikesComponent {
  readonly why = input.required<NoStrikesWhy>();
  /** Contracts listed, as the server counted them. */
  readonly calls = input.required<number>();
  readonly puts = input.required<number>();
  /** The download card above offers to fetch what is missing. */
  readonly downloadOffered = input(false);
}
