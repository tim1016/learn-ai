import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceBotRoute,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';

/** What one bot's control is: Stop while it runs, Flatten… while it is
 * stopped still holding money, and nothing once it is done. */
type BotControl = 'stop' | 'flatten' | null;

/** Where the page's Stop for one bot stands: its Clerk's offer is being
 * read, or the Stop is being sent. */
export type StopPhase = 'reading' | 'sending';

/**
 * One bot's command on Home, the same on a List row and a Wall tile.
 *
 * Stop hands the bot to the page, which owns the one action path: it reads
 * the stop the bot's Clerk offers now and asks with that action's own
 * confirmation — the dialog the bot page shows — before anything is sent
 * (#2605). Flatten… opens the bot's page, where the stopped-but-holding
 * warning, the prepared plan and its confirmation live: a flatten is
 * prepared from fresh evidence, never fired from a list.
 */
@Component({
  selector: 'app-home-bot-action',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './home-bot-action.component.html',
  styleUrl: './home-bot-action.component.scss',
})
export class HomeBotActionComponent {
  readonly bot = input.required<BotCatalogView>();
  readonly account = input.required<BoundAccountWorkspaceAddress>();
  /** Where the page's Stop for this bot stands, if it is in flight. */
  readonly stopPhase = input<StopPhase | null>(null);
  readonly stopRequested = output<string>();

  protected readonly control = computed<BotControl>(() => {
    const bot = this.bot();
    if (bot.running) return 'stop';
    return bot.group === 'holding' ? 'flatten' : null;
  });

  protected readonly botLink = computed(
    () => accountWorkspaceBotRoute(this.account(), this.bot().strategy_instance_id).commands,
  );
}
