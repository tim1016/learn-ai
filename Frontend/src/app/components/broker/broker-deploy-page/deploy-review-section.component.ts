import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { DeployBotStrategy, DeployBotView } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-review-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './deploy-review-section.component.html',
  styleUrl: './deploy-review-section.component.scss',
})
export class DeployReviewSectionComponent {
  readonly view = input.required<DeployBotView>();
  readonly strategy = input<DeployBotStrategy | null>(null);
  readonly instanceId = input.required<string>();
  readonly symbol = input.required<string>();
  readonly sizingLabel = input.required<string>();
  readonly quantity = input.required<number>();
  readonly carryoverAllowed = input.required<boolean>();
}
