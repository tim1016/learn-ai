import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceBotRoute,
  accountWorkspaceDeployAgainRoute,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';

/**
 * Home's Finished fold (PRD #2560 D7): bots that are stopped, flat and fully
 * released, which move here by themselves — there is no manual archive. Each
 * shows when it ended, its trades, its whole-life result (Python-authored, or
 * "unknown" when the fee evidence cannot vouch for it — never $0) and Deploy
 * again. Folded by default.
 */
@Component({
  selector: 'app-home-finished',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, RouterLink, TimestampDisplayComponent],
  templateUrl: './home-finished.component.html',
  styleUrl: './home-finished.component.scss',
})
export class HomeFinishedComponent {
  readonly bots = input.required<readonly BotCatalogView[]>();
  readonly account = input.required<BoundAccountWorkspaceAddress>();

  protected readonly rows = computed(() =>
    this.bots().map((bot) => ({
      bot,
      page: accountWorkspaceBotRoute(this.account(), bot.strategy_instance_id).commands,
      again: accountWorkspaceDeployAgainRoute(this.account(), bot.strategy_instance_id),
    })),
  );
}
