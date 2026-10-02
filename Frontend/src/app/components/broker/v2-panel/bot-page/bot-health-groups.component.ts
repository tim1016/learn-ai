import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotHealthGroupsView, HealthLineView, MarketPulseView } from '../lib/broker-v2-panel.types';

/**
 * The bot's health in two groups (#2794 R9): what happened during this run,
 * and the account right now, then the market for its symbol. Each line is
 * the backend's -- its state, value and note -- so an account condition reads
 * as the account's, and a stopped bot's lines say when they do not affect it.
 */
@Component({
  selector: 'app-bot-health-groups',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './bot-health-groups.component.html',
  styleUrl: './bot-health-groups.component.scss',
})
export class BotHealthGroupsComponent {
  readonly health = input.required<BotHealthGroupsView>();
  /** The market for this bot's symbol right now, as the backend words it. */
  readonly marketPulse = input<MarketPulseView | null>(null);

  protected readonly groups = computed((): readonly { title: string; lines: readonly HealthLineView[] }[] => [
    { title: 'During this run', lines: this.health().run },
    { title: 'Account right now', lines: this.health().account },
  ]);
}
