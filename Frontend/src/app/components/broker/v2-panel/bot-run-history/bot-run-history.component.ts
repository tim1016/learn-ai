import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { AccountWorkspaceLink } from '../../../../fleet/account-workspace';
import type { CurrentRunState, FeedContinuityView } from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BotRunEvidenceCardComponent } from './bot-run-evidence-card.component';

/**
 * The bot page's run evidence: the current run's launch, process and
 * terminal evidence, and its IBKR market-data continuity. Earlier runs --
 * and every other bot -- are listed in History (#2574), which this links to;
 * the one-run-at-a-time pager it replaced is gone.
 */
@Component({
  selector: 'app-bot-run-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotRunEvidenceCardComponent, RouterLink, TimestampDisplayComponent],
  templateUrl: './bot-run-history.component.html',
  styleUrl: './bot-run-history.component.scss',
})
export class BotRunHistoryComponent {
  readonly state = input.required<CurrentRunState>();
  readonly botRunning = input(false);
  readonly feedContinuity = input.required<FeedContinuityView>();
  /** History, where every run of every bot is listed. */
  readonly historyLink = input.required<AccountWorkspaceLink>();
  readonly retryRequested = output();

  protected readonly evidenceUpdatedAtMs = computed<number | null>(() => {
    const run = this.state().run;
    if (!run) return null;
    return run.terminal_outcome?.recorded_at_ms ?? run.process.observed_at_ms;
  });
}
