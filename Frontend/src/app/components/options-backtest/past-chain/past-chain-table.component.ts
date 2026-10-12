import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ContractCellComponent } from './contract-cell.component';
import { ExpiryTagComponent } from './expiry-tag.component';
import { NoStrikesComponent } from './no-strikes.component';
import { bodyOf, type ChainView, type Gap } from './past-chain-view';
import { UnderlyingStripComponent } from './underlying-strip.component';

/** The words for the control a chain waits on. */
const CONTROL_NAMES = { underlying: 'underlying', date: 'trading date', time: 'time of day', strikes: 'strikes box' } as const;

/** What an empty cell says, by why it is empty. */
const GAP_WORDS: Readonly<Record<Gap, string>> = {
  not_listed: 'Not listed at this strike',
  no_other: 'No other contract at this strike',
  outside: 'Nothing in this window',
};

/**
 * One expiry as of one instant: the underlying's own price with its time, then calls on the left, puts on the right
 * and the strikes down the middle, the money strike marked as the server served it. Each contract says in words which
 * of four things is true of it; none is ever a blank or a zero. When no strikes can be drawn the card says why, from
 * the underlying's state, and never that nothing is listed.
 */
@Component({
  selector: 'app-options-backtest-past-chain-table',
  imports: [AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent, ContractCellComponent, ExpiryTagComponent, NoStrikesComponent, UnderlyingStripComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './past-chain-table.component.html',
  styleUrls: ['../options-backtest-card.scss', './past-chain.scss', './past-chain-table.component.scss'],
})
export class PastChainTableComponent {
  readonly view = input.required<ChainView>();
  /** The `expiry_ms` that was picked: said back when the date does not list it. */
  readonly expiryMs = input<number | null>(null);
  /** The chain on screen is the last answer, while the same question is read again. */
  readonly again = input(false);
  /** The download card offers to fetch what is missing, so the card can point to it. */
  readonly downloadOffered = input(false);
  readonly retry = output();

  /** The chain on screen, with what its body comes to. */
  protected readonly card = computed(() => {
    const view = this.view();
    return view.kind === 'shown' ? { chain: view.chain, body: bodyOf(view.chain) } : null;
  });
  /** The control the chain is waiting on, in words. */
  protected readonly waitingOn = computed(() => {
    const view = this.view();
    return view.kind === 'blocked' ? CONTROL_NAMES[view.control] : null;
  });

  protected gapWords(gap: Gap | null): string {
    return gap === null ? '' : GAP_WORDS[gap];
  }
}
