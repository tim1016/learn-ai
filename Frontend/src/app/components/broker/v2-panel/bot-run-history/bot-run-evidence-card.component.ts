import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotRunView, RunHistoryMode } from '../lib/broker-v2-panel.types';

@Component({
  selector: 'app-bot-run-evidence-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './bot-run-evidence-card.component.html',
  styleUrl: './bot-run-evidence-card.component.scss',
})
export class BotRunEvidenceCardComponent {
  readonly run = input.required<BotRunView>();
  readonly mode = input.required<RunHistoryMode>();
}
