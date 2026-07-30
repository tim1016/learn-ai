import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { OfflineReplayBotResult } from '../../../api/offline-replay.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

@Component({
  selector: 'app-offline-replay-bot-card',
  imports: [DecimalPipe, CurrencyPipe, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './offline-replay-bot-card.component.html',
  styleUrl: './offline-replay-bot-card.component.scss',
})
export class OfflineReplayBotCardComponent {
  readonly bot = input.required<OfflineReplayBotResult>();

  readonly countMetrics = computed(() => {
    const bot = this.bot();
    return [
      { label: 'Source bars', value: bot.bars_processed },
      { label: 'Decisions', value: bot.decisions_emitted },
      { label: 'Orders', value: bot.orders_submitted },
      { label: 'Fills', value: bot.fills_observed },
      { label: 'Open quantity', value: bot.open_position_quantity },
    ];
  });
}
