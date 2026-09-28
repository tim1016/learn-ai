import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceBotRoute,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { fmtSignedCurrency } from '../../broker/format';
import { MoneyBarComponent } from '../../broker/money-bar/money-bar.component';
import { HomeBotActionComponent } from './home-bot-action.component';
import { homeBotHue, homeBotStrip, type HomeBot } from './home-bots';

/**
 * One bot on Home's List (PRD #2560 "Bots on Home").
 *
 * A running row shows its budget strip — its own slice of the account's bar —
 * with its balance, free budget, today's P&L and Stop. A holding row is
 * striped, says what it still holds and that no bot manages it, and offers
 * Flatten…. A Dry Run row shows its simulated starting cash and simulated
 * P&L, never the account's money. Every dollar is a Python-authored string;
 * the row adds nothing up. The row opens the bot's page.
 */
@Component({
  selector: 'app-home-bot-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, HomeBotActionComponent, MoneyBarComponent, RouterLink],
  templateUrl: './home-bot-row.component.html',
  styleUrl: './home-bot-row.component.scss',
  host: {
    '[class.home-row--holding]': "entry().bot.group === 'holding'",
    '[class.home-row--dry-run]': "entry().bot.group === 'dry_run'",
  },
})
export class HomeBotRowComponent {
  readonly entry = input.required<HomeBot>();
  readonly account = input.required<BoundAccountWorkspaceAddress>();
  readonly pending = input(false);
  readonly stopRequested = output<string>();

  protected readonly botLink = computed(
    () => accountWorkspaceBotRoute(this.account(), this.entry().bot.strategy_instance_id).commands,
  );

  /** The strip draws this bot's own slice and nothing else. */
  protected readonly strip = computed(() => homeBotStrip(this.entry()));
  protected readonly hue = computed(() => homeBotHue(this.entry()));
  protected readonly stripCaption = computed(() => `Money held by ${this.entry().bot.strategy_instance_id}`);

  /** The P&L beside the row, and what it is: today's for a running bot (or a
   * Dry Run's, simulated), the shares' open P&L for a stopped one. */
  protected readonly pnl = computed(() => {
    const bot = this.entry().bot;
    if (bot.group === 'holding') return { value: fmtSignedCurrency(bot.open_pnl), caption: 'open' };
    return {
      value: fmtSignedCurrency(bot.day_pnl),
      caption: bot.group === 'dry_run' ? 'today, simulated' : 'today',
    };
  });
}
