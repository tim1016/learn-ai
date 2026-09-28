import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { DEPLOYMENT_WORLD_LABELS, type BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-launch-receipt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, ReceiptLabelPipe, RouterLink, TimestampDisplayComponent],
  templateUrl: './deploy-launch-receipt.component.html',
  styleUrl: './deploy-launch-receipt.component.scss',
})
export class DeployLaunchReceiptComponent {
  protected readonly worldLabels = DEPLOYMENT_WORLD_LABELS;
  readonly receipt = input.required<BudgetDeployReceipt>();
}
