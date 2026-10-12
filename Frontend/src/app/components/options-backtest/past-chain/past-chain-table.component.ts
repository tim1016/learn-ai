import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ContractCellComponent } from './contract-cell.component';
import { ExpiryTagComponent } from './expiry-tag.component';
import { strikeRows, underlyingViewOf, type ChainView } from './past-chain-view';

/** The words for the control a chain waits on. */
const CONTROL_NAMES = { underlying: 'underlying', date: 'trading date', time: 'time of day', strikes: 'strikes box' } as const;

/**
 * One expiry as of one instant: the underlying's own price with its time, then calls on the left, puts on the right
 * and the strikes down the middle, the money strike marked as the server served it. Each contract says in words which
 * of four things is true of it; none is ever a blank or a zero.
 */
@Component({
  selector: 'app-options-backtest-past-chain-table',
  imports: [AssetIdentityComponent, AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent, ContractCellComponent, ExpiryTagComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './past-chain-table.component.html',
  styleUrls: ['../options-backtest-card.scss', './past-chain.scss', './past-chain-table.component.scss'],
})
export class PastChainTableComponent {
  readonly view = input.required<ChainView>();
  /** The `expiry_ms` that was picked: said back when the date does not list it. */
  readonly expiryMs = input<number | null>(null);
  readonly retry = output();

  protected readonly shown = computed(() => {
    const view = this.view();
    return view.kind === 'shown' ? view.chain : null;
  });
  protected readonly rows = computed(() => {
    const chain = this.shown();
    return chain === null ? [] : strikeRows(chain.calls, chain.puts);
  });
  protected readonly underlying = computed(() => {
    const chain = this.shown();
    return chain === null ? null : underlyingViewOf(chain.underlying_price);
  });
  /** The control the chain is waiting on, in words. */
  protected readonly waitingOn = computed(() => {
    const view = this.view();
    return view.kind === 'blocked' ? CONTROL_NAMES[view.control] : null;
  });
}
