import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import { accountWorkspaceHistoryLink } from '../../../../fleet/account-workspace';
import type { CurrentRunState, FeedContinuityView } from '../lib/broker-v2-panel.types';
import { EMPTY_CURRENT_RUN_STATE } from '../lib/broker-v2-panel.types';
import { OperatorDisclosureCardComponent } from '../operator-lens/operator-disclosure-card.component';
import { BotRunHistoryComponent } from './bot-run-history.component';

/** The bot page's folded run evidence: the current run, and the way to
 * History for every earlier one (#2574). */
@Component({
  selector: 'app-operator-run-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotRunHistoryComponent, OperatorDisclosureCardComponent],
  templateUrl: './operator-run-history.component.html',
})
export class OperatorRunHistoryComponent {
  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  readonly botRunning = input.required<boolean>();
  readonly currentRunState = input<CurrentRunState>(EMPTY_CURRENT_RUN_STATE);
  readonly runRefreshRequested = output();
  readonly feedContinuity = input.required<FeedContinuityView>();
  protected readonly expanded = signal(false);

  /** History, narrowed to this account's bots. */
  protected readonly historyLink = computed(() => accountWorkspaceHistoryLink(
    { broker: this.broker(), clerkId: this.clerkId(), accountId: this.accountId() },
    { account: this.clerkId() },
  ));
}
