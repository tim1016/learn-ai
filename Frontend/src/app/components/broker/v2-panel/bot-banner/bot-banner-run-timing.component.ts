import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotHealthCard, CurrentRunState } from '../lib/broker-v2-panel.types';

/**
 * The banner's run-timing strip: Started/Ended for both lenses, plus the
 * operator-only strategy-activity clocks (last bar, last decision, and
 * staleness) and a retry affordance when the run resource failed to load.
 * Split out of `BotBannerComponent` to keep that template under the
 * repository's ~80-line guideline.
 */
@Component({
  selector: 'app-bot-banner-run-timing',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './bot-banner-run-timing.component.html',
  styleUrl: './bot-banner-run-timing.component.scss',
})
export class BotBannerRunTimingComponent {
  readonly runState = input.required<CurrentRunState>();
  readonly health = input.required<BotHealthCard>();
  readonly operator = input(false);

  readonly retryRequested = output();
}
