import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotHealthCard, CurrentRunState } from '../lib/broker-v2-panel.types';

/** Prominent run timing; terminal timestamps come only from the run receipt. */
@Component({
  selector: 'app-bot-run-timing',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './bot-run-timing.component.html',
  styleUrl: './bot-run-timing.component.scss',
})
export class BotRunTimingComponent {
  readonly state = input.required<CurrentRunState>();
  readonly health = input.required<BotHealthCard>();
  readonly operator = input(false);
  readonly retryRequested = output();
}
