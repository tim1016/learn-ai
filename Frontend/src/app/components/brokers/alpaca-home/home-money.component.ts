import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { MoneyBarComponent } from '../../broker/money-bar/money-bar.component';
import type { AccountMoneyView } from '../../broker/v2-panel/lib/broker-v2-panel.service';

/**
 * "Where the money is" (PRD #2560 D1, D6, D12): the account's money bar at
 * Home size with its legend, the total and what it adds up to, and open P&L
 * as a note beside the bar — never a slice of it. Every figure is the
 * account-money read's own string; when the read cannot draw the bar, its
 * reason is shown instead, never a $0.
 */
@Component({
  selector: 'app-home-money',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, MoneyBarComponent],
  templateUrl: './home-money.component.html',
  styleUrl: './home-money.component.scss',
})
export class HomeMoneyComponent {
  /** The account-money read, or `null` while it has not answered. */
  readonly view = input.required<AccountMoneyView | null>();
  /** What to say while there is no read: still reading, or it failed. */
  readonly unread = input.required<string>();
}
