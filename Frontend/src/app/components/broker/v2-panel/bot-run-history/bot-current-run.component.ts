import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { AccountWorkspaceLink } from '../../../../fleet/account-workspace';
import type { CurrentRunState, FeedContinuityView } from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BotRunEvidenceCardComponent } from './bot-run-evidence-card.component';

/**
 * The bot page's current run: its launch, process and terminal evidence, and
 * its IBKR market-data continuity. The bot's earlier runs are listed in
 * History (#2574), which this links to, narrowed to this bot.
 */
@Component({
  selector: 'app-bot-current-run',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotRunEvidenceCardComponent, RouterLink, TimestampDisplayComponent],
  templateUrl: './bot-current-run.component.html',
  styleUrl: './bot-current-run.component.scss',
})
export class BotCurrentRunComponent {
  readonly state = input.required<CurrentRunState>();
  readonly botRunning = input(false);
  readonly feedContinuity = input.required<FeedContinuityView>();
  /** History, narrowed to this bot and every one of its runs. */
  readonly historyLink = input.required<AccountWorkspaceLink>();
  readonly retryRequested = output();

  protected readonly evidenceUpdatedAtMs = computed<number | null>(() => {
    const run = this.state().run;
    if (!run) return null;
    return run.terminal_outcome?.recorded_at_ms ?? run.process.observed_at_ms;
  });
}
