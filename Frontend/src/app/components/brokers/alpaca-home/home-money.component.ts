import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { MoneyBarComponent } from '../../broker/money-bar/money-bar.component';
import type { AccountMoneyState } from '../../broker/v2-panel/lib/account-money-state';

/**
 * "Where the money is" (PRD #2560 D1, D6, D12): the account's money bar at
 * Home size with its legend, the total and what it adds up to, and the bar's
 * notes — a shortfall, open P&L or why there is none — beside it, never as a
 * slice. It renders the account's one money state (`accountMoneyState`):
 * every figure is the backend's own string, and a state that cannot draw the
 * bar says why instead, never $0.
 *
 * When Deploy would refuse a new bot on this money, the backend's refusal
 * sits beside the bar, so free to deploy never reads as spendable when it is
 * not (PRD #2560 story 12).
 */
@Component({
  selector: 'app-home-money',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, MoneyBarComponent, RouterLink],
  templateUrl: './home-money.component.html',
  styleUrl: './home-money.component.scss',
})
export class HomeMoneyComponent {
  readonly state = input.required<AccountMoneyState>();
  /** The lane's Settings, where a refusal's cure lives; `null` when this
   * workspace has none to offer. */
  readonly settingsRoute = input<readonly string[] | null>(null);
}
