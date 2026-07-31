import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { AlpacaBotControlFixture } from './alpaca-bot-control-fixtures';

@Component({
  selector: 'app-alpaca-bot-control-operator-diagnostic',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './alpaca-bot-control-operator-diagnostic.component.html',
  styleUrl: './alpaca-bot-control-operator-diagnostic.component.scss',
})
export class AlpacaBotControlOperatorDiagnosticComponent {
  readonly fixture = input.required<AlpacaBotControlFixture>();
}
