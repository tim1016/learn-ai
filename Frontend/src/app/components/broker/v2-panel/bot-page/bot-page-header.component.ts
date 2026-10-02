import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { accountWorkspaceHistoryLink } from '../../../../fleet/account-workspace';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { ExperimentalNoticeComponent } from '../../../../shared/experimental-notice/experimental-notice.component';
import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import type { BotPanelView } from '../lib/broker-v2-panel.types';

/** One holding as the header names it: "1 SPY". */
interface HeldFigure {
  readonly symbol: string;
  readonly quantity: number;
}

/**
 * The bot page's banner (#2794 R1, R3, R9): which bot this is and the way
 * back, one status the bot owns, the backend's one-line summary of its run,
 * and its key figures. It carries no LIVE chip -- the top bar and account
 * strip say that -- no account-scoped verdict and no ticking time.
 *
 * A cleared bot's page, opened from History (#2574), says it was cleared and
 * links back to History. A strategy that is not a trading strategy says so
 * under its name, in its registry entry's words (#2607).
 */
@Component({
  selector: 'app-bot-page-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, AuthoredUsdPipe, ExperimentalNoticeComponent, RouterLink],
  templateUrl: './bot-page-header.component.html',
  styleUrl: './bot-page-header.component.scss',
})
export class BotPageHeaderComponent {
  readonly panel = input.required<BotPanelView>();
  readonly backRoute = input.required<readonly string[]>();
  readonly backLabel = input.required<string>();
  readonly clerkId = input.required<string>();

  protected readonly page = computed(() => this.panel().bot_page ?? null);
  protected readonly held = computed((): readonly HeldFigure[] =>
    Object.entries(this.panel().exposure)
      .map(([symbol, quantity]) => ({ symbol, quantity }))
      .sort((left, right) => left.symbol.localeCompare(right.symbol)),
  );
  protected readonly cleared = computed(() => this.panel().status === 'cleared');
  protected readonly history = computed(() => accountWorkspaceHistoryLink(
    { broker: this.panel().broker, clerkId: this.clerkId() },
    { status: 'cleared' },
  ));
}
