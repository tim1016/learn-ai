import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { accountWorkspaceHistoryLink } from '../../../../fleet/account-workspace';
import { ExperimentalNoticeComponent } from '../../../../shared/experimental-notice/experimental-notice.component';
import type { BotPanelView } from '../lib/broker-v2-panel.types';

/**
 * What the bot page says under the tabs, and only when it applies: why the
 * bot needs attention, that its strategy is not a trading strategy (#2607),
 * and that it was cleared, with the way to History (#2574). The banner in the
 * workspace header has no room for a sentence; these each need one.
 */
@Component({
  selector: 'app-bot-page-notices',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ExperimentalNoticeComponent, RouterLink],
  templateUrl: './bot-page-notices.component.html',
  styleUrl: './bot-page-notices.component.scss',
})
export class BotPageNoticesComponent {
  readonly panel = input.required<BotPanelView>();
  readonly clerkId = input.required<string>();

  /** The backend's reason, which it gives only when the bot needs attention. */
  protected readonly attentionReason = computed(() => this.panel().bot_page?.status.reason ?? null);
  protected readonly cleared = computed(() => this.panel().status === 'cleared');
  protected readonly history = computed(() => accountWorkspaceHistoryLink(
    { broker: this.panel().broker, clerkId: this.clerkId() },
    { status: 'cleared' },
  ));
}
