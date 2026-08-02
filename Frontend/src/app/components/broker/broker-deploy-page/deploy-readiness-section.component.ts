import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import {
  ReceiptLabelPipe,
  formatReceiptValue,
} from '../../../shared/pipes/receipt-label.pipe';
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

  protected evidenceEntries(
    check: DeployReadinessCheck,
  ): [string, string][] {
    return Object.entries(check.evidence ?? {}).map(([label, value]) => [
      label,
      formatReceiptValue(label, value === null ? 'Not recorded' : String(value)),
    ]);
  }
}
