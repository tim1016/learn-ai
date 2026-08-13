import {
  ChangeDetectionStrategy,
  Component,
  input,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * Shared structural frame for a bot-detail lens banner.
 *
 * The shell owns only navigation and the snapshot freshness stamp. Each lens
 * projects its own identity, quote, mission-status, and action content so the
 * trader and operator views remain purpose-built rather than accumulating
 * flags in a common header model.
 */
@Component({
  selector: 'app-bot-detail-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, TimestampDisplayComponent],
  templateUrl: './bot-detail-banner.component.html',
  styleUrl: './bot-detail-banner.component.scss',
})
export class BotDetailBannerComponent {
  readonly backLink = input.required<string | string[]>();
  readonly backLabel = input.required<string>();
  readonly updatedAtMs = input.required<number>();
  /** Screen-reader summary of the latest panel revision, without visual header noise. */
  readonly snapshotStatus = input<string | null>(null);
}
