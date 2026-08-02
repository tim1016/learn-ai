import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { RunAdmissionDecision } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-start-admission',
  imports: [TimestampDisplayComponent],
  templateUrl: './deploy-start-admission.component.html',
  styleUrl: './deploy-start-admission.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DeployStartAdmissionComponent {
  readonly decision = input.required<RunAdmissionDecision>();
}
