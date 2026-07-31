import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { AlpacaBotControlFixture } from './alpaca-bot-control-fixtures';

@Component({
  selector: 'app-alpaca-bot-control-trader-diagnostic',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './alpaca-bot-control-trader-diagnostic.component.html',
  styleUrl: './alpaca-bot-control-trader-diagnostic.component.scss',
})
export class AlpacaBotControlTraderDiagnosticComponent {
  readonly fixture = input.required<AlpacaBotControlFixture>();
}
