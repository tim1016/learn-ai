import {
  ChangeDetectionStrategy,
  Component,
  input,
} from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * Shared structural frame for a bot-detail lens banner.
 *
 * The shell owns only the snapshot freshness stamp. Each lens projects its own
 * identity, quote, mission-status, and action content so the trader and
 * operator views remain purpose-built rather than accumulating flags in a
 * common header model.
 *
 * It carries no way back: a bot's page sits inside the account workspace
 * (ADR 0064 Decision 1), whose own back link knows which tab the page was
 * opened from. This one always pointed at the roster, which was wrong for
 * every bot opened from the Gallery.
 */
@Component({
  selector: 'app-bot-detail-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './bot-detail-banner.component.html',
  styleUrl: './bot-detail-banner.component.scss',
})
export class BotDetailBannerComponent {
  readonly updatedAtMs = input.required<number>();
  /** Screen-reader summary of the latest panel revision, without visual header noise. */
  readonly snapshotStatus = input<string | null>(null);
}
