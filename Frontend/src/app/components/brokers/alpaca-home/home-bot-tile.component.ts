import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceBotRoute,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import { fmtSignedCurrency } from '../../broker/format';
import { MoneyBarComponent } from '../../broker/money-bar/money-bar.component';
import type { ChartBar, ChartFillMarker } from '../../broker/v2-panel/gallery/lib/gallery.types';
import { HomeBotActionComponent } from './home-bot-action.component';
import { homeBotHue, homeBotStrip, type HomeBot } from './home-bots';
import { HomeSparklineComponent } from './home-sparkline.component';

/** A tile's group, said in words: a status is never a border colour alone. */
const GROUP_WORDS: Readonly<Record<HomeBot['bot']['group'], string>> = {
  running: 'RUNNING',
  holding: 'STOPPED · STILL HOLDING',
  dry_run: 'DRY RUN',
  finished: 'FINISHED',
};

/**
 * One bot on Home's Wall (PRD #2560 D11): the List's row as a chart tile —
 * today's candles and fills from the gallery live feed, the bot's budget
 * strip, and the same command the row offers. Tiles are placed in exactly
 * the List's order; nothing here can move one.
 */
@Component({
  selector: 'app-home-bot-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, HomeBotActionComponent, HomeSparklineComponent, MoneyBarComponent, RouterLink],
  templateUrl: './home-bot-tile.component.html',
  styleUrl: './home-bot-tile.component.scss',
  host: {
    '[class.home-tile--holding]': "entry().bot.group === 'holding'",
    '[class.home-tile--dry-run]': "entry().bot.group === 'dry_run'",
  },
})
export class HomeBotTileComponent {
  readonly entry = input.required<HomeBot>();
  readonly account = input.required<BoundAccountWorkspaceAddress>();
  readonly bars = input<readonly ChartBar[]>([]);
  readonly markers = input<readonly ChartFillMarker[]>([]);
  readonly pending = input(false);
  readonly stopRequested = output<string>();

  protected readonly botLink = computed(
    () => accountWorkspaceBotRoute(this.account(), this.entry().bot.strategy_instance_id).commands,
  );
  protected readonly groupWords = computed(() => GROUP_WORDS[this.entry().bot.group]);
  protected readonly strip = computed(() => homeBotStrip(this.entry()));
  protected readonly hue = computed(() => homeBotHue(this.entry()));
  protected readonly pnl = computed(() => {
    const bot = this.entry().bot;
    return fmtSignedCurrency(bot.group === 'holding' ? bot.open_pnl : bot.day_pnl);
  });
}
