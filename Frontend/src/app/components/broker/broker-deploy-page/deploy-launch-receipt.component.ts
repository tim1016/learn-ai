import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { DeployBotReceipt } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-launch-receipt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, TimestampDisplayComponent],
  templateUrl: './deploy-launch-receipt.component.html',
  styleUrl: './deploy-launch-receipt.component.scss',
})
export class DeployLaunchReceiptComponent {
  readonly receipt = input.required<DeployBotReceipt>();
}
