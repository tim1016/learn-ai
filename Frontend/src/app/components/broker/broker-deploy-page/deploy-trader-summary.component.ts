import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { DeployBotStrategy, DeployBotView } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-trader-summary',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './deploy-trader-summary.component.html',
  styleUrl: './deploy-trader-summary.component.scss',
})
export class DeployTraderSummaryComponent {
  readonly view = input.required<DeployBotView>();
  readonly strategy = input<DeployBotStrategy | null>(null);
  readonly symbol = input.required<string>();
  readonly quantity = input.required<number>();
  readonly carryoverAllowed = input.required<boolean>();
  readonly operatorRequested = output();
}
