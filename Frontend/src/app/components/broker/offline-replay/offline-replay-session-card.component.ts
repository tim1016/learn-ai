import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { OfflineReplaySession } from '../../../api/offline-replay.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { OfflineReplayBotCardComponent } from './offline-replay-bot-card.component';
import {
  OfflineReplayCommand,
  OfflineReplayTransportComponent,
} from './offline-replay-transport.component';

@Component({
  selector: 'app-offline-replay-session-card',
  imports: [
    ReceiptLabelPipe,
    TimestampDisplayComponent,
    OfflineReplayTransportComponent,
    OfflineReplayBotCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './offline-replay-session-card.component.html',
  styleUrl: './offline-replay-session-card.component.scss',
})
export class OfflineReplaySessionCardComponent {
  readonly session = input.required<OfflineReplaySession>();
  readonly active = input.required<boolean>();
  readonly acting = input.required<boolean>();
  readonly replayCommand = output<OfflineReplayCommand>();

  readonly visibleBarsProcessed = computed(() => {
    const session = this.session();
    if (session.bots.length === 0) return 0;
    const warmupBars = Math.round(
      (session.warmup_end_ms - session.session_open_ms) / 60_000,
    );
    return Math.max(
      0,
      Math.min(...session.bots.map((bot) => bot.bars_processed)) - warmupBars,
    );
  });

  readonly progressPercent = computed(() =>
    Math.min(100, (this.visibleBarsProcessed() / this.session().playback_minutes) * 100),
  );
}
