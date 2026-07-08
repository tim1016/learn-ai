import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { GateStep } from '../../../../../api/live-runs.types';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import {
  type DisplayRow,
  type FactEntry,
  gateFacts,
} from './bot-event-display-row';

@Component({
  selector: 'app-bot-event-drawer',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-event-drawer.component.html',
  styleUrl: './bot-event-drawer.component.scss',
})
export class BotEventDrawerComponent {
  readonly display = input.required<DisplayRow>();
  readonly drawerId = input.required<string>();

  gateFacts(step: GateStep): readonly FactEntry[] {
    return gateFacts(step);
  }

  trackGate(index: number, step: GateStep): string {
    return `${step.evaluation_id}:${step.gate_id}:${step.gate_result}:${index}`;
  }

  trackFact(_index: number, entry: FactEntry): string {
    return entry.key;
  }
}
