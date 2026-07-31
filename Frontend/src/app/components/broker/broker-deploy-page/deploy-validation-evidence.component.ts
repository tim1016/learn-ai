import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { DeployBotStrategy } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-validation-evidence',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './deploy-validation-evidence.component.html',
  styleUrl: './deploy-validation-evidence.component.scss',
})
export class DeployValidationEvidenceComponent {
  readonly strategy = input<DeployBotStrategy | null>(null);
}
