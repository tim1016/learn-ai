import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { NextAttemptComponent } from '../../shared/next-attempt/next-attempt.component';
import type {
  BotHealthCard,
  BotPanelView,
  BotHealthGroupsView,
  HealthLineView,
  MarketPulseView,
  StartupJoinView,
} from '../lib/broker-v2-panel.types';
import { ExposureNoticesComponent } from '../startup-join/exposure-notices.component';
import { StartupJoinStatusComponent } from '../startup-join/startup-join-status.component';

/**
 * The bot's health in two groups (#2794 R9): what happened during this run,
 * and the account right now, then the market for its symbol. Each line is
 * the backend's -- its state, value and note -- so an account condition reads
 * as the account's, and a stopped bot's lines say when they do not affect it.
 * Under the run: how it is still joining its warmup to the live stream, or
 * how it ended and what that left at the broker. Under the account, while
 * the Clerk needs the operator: its explanation, next step and next
 * automatic retry -- the words an exit that did not flatten carries (#2440).
 */
@Component({
  selector: 'app-bot-health-groups',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ExposureNoticesComponent, NextAttemptComponent, StartupJoinStatusComponent, TimestampDisplayComponent],
  templateUrl: './bot-health-groups.component.html',
  styleUrl: './bot-health-groups.component.scss',
})
export class BotHealthGroupsComponent {
  readonly health = input.required<BotHealthGroupsView>();
  /** The market for this bot's symbol right now, as the backend words it. */
  readonly marketPulse = input<MarketPulseView | null>(null);
  /** How the run is joining its warmup to the live stream (#2410), while it says. */
  readonly startupJoin = input<StartupJoinView | null>(null);
  /** How the run ended, and what it left at the broker, as the runner recorded it. */
  readonly dutyOutcome = input<BotHealthCard['duty_outcome']>(null);
  /** The Clerk's word on the account while it needs the operator: what is wrong, the next step, the next retry. */
  readonly clerkGuidance = input<BotPanelView['mission_verdict'] | null>(null);

  /** The guidance's explanation, unless an account line already says it. */
  protected readonly guidanceExplanation = computed(() => {
    const explanation = this.clerkGuidance()?.explanation ?? null;
    const said = new Set(this.health().account.map((line) => line.note));
    return explanation !== null && !said.has(explanation) ? explanation : null;
  });

  protected readonly groups = computed(
    (): readonly { key: 'run' | 'account'; title: string; lines: readonly HealthLineView[] }[] => [
      { key: 'run', title: 'During this run', lines: this.health().run },
      { key: 'account', title: 'Account right now', lines: this.health().account },
    ],
  );
}
