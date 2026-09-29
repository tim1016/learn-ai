import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { accountWorkspaceBotRoute } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneConfirmedAccount, laneDisplayNameText } from '../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService, verdictModeChip } from '../../../services/alpaca-live-verdict.service';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { AlpacaLaneModeChipComponent } from '../alpaca-desk/alpaca-lane-mode-chip.component';
import type { FleetBotHistoryRow } from './bot-history.service';

/**
 * History's list (#2574): one line per bot, newest first. Each line names its
 * account with the lane colour and the mode worded, and opens the bot's own
 * page. Every figure is the backend's: counts as counted, dollars as authored
 * strings (an unknown one says so, never $0), outcomes in the backend's
 * words with their code through `receiptLabel`.
 *
 * A bot the retired Resume ran several times expands to one line per run;
 * its result and fees stay on the bot's line, because they are the bot's
 * alone. A bot whose money is unknown expands to say why.
 */
@Component({
  selector: 'app-history-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaLaneModeChipComponent,
    AssetIdentityComponent,
    AuthoredUsdPipe,
    ReceiptLabelPipe,
    RouterLink,
    TimestampDisplayComponent,
  ],
  templateUrl: './history-table.component.html',
  styleUrl: './history-table.component.scss',
})
export class HistoryTableComponent {
  readonly bots = input.required<readonly FleetBotHistoryRow[]>();

  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);

  private readonly opened = signal<ReadonlySet<string>>(new Set());

  protected readonly rows = computed(() =>
    this.bots().map((bot) => {
      const key = `${bot.clerk_id}/${bot.strategy_instance_id}`;
      const lane = this.fleetDirectory.lane(bot.broker, bot.clerk_id);
      const name = this.fleetDirectory.displayNameOf(bot.broker, bot.clerk_id);
      const expandable = bot.runs.length > 1 || bot.money_unavailable_reason !== null;
      return {
        key,
        bot,
        page: accountWorkspaceBotRoute({
          broker: bot.broker,
          clerkId: bot.clerk_id,
          accountId: (lane === undefined ? null : laneConfirmedAccount(lane)) ?? bot.account_id,
        }, bot.strategy_instance_id).commands,
        accountName: name === null ? bot.account_id : laneDisplayNameText(name),
        mode: verdictModeChip(this.liveVerdicts.stateFor(bot.clerk_id)),
        expandable,
        open: expandable && this.opened().has(key),
        toggleLabel: bot.runs.length > 1 ? `${bot.runs.length} runs` : 'Why unknown',
        runs: bot.runs.map((run, index) => ({ run, label: `Run ${bot.runs.length - index}` })),
      };
    }),
  );

  protected toggle(key: string): void {
    this.opened.update((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  }
}
