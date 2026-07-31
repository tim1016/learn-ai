import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { DeployReadinessCheck } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-readiness-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './deploy-readiness-section.component.html',
  styleUrl: './deploy-readiness-section.component.scss',
})
export class DeployReadinessSectionComponent {
  readonly checks = input.required<DeployReadinessCheck[]>();
}
