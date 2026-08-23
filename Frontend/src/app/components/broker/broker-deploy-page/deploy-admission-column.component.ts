import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type {
  DeployBotView,
  RunAdmissionDecision,
} from '../v2-panel/lib/broker-v2-panel.service';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { DeployReadinessSectionComponent } from './deploy-readiness-section.component';
import { DeployStartAdmissionComponent } from './deploy-start-admission.component';

/** A refused deployment, shaped for display by the workflow that caught it. */
export interface DeployError {
  outcome: 'conflict' | 'blocked' | 'unknown';
  title: string;
  message: string;
  explanation: string | null;
  nextAction: string | null;
  receiptId: string | null;
  recordedAtMs: number | null;
}

/** Ready/blocked plus the gate count that backs it. */
export interface DeployReadinessSummary {
  readonly label: string;
  readonly counts: string;
}

/**
 * Live admission for a deployment, always on screen.
 *
 * It used to sit behind an "Operator" tab whose sibling "Trader" tab rendered
 * nothing, so the toggle only ever hid the reason a launch was refused.
 */
@Component({
  selector: 'app-deploy-admission-column',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    TimestampDisplayComponent,
    DeployStartAdmissionComponent,
    DeployReadinessSectionComponent,
  ],
  templateUrl: './deploy-admission-column.component.html',
  styleUrl: './deploy-admission-column.component.scss',
})
export class DeployAdmissionColumnComponent {
  readonly view = input.required<DeployBotView>();
  readonly summary = input<DeployReadinessSummary | null>(null);
  readonly submitError = input<DeployError | null>(null);
  readonly admissionDecision = input<RunAdmissionDecision | null>(null);

  protected readonly operatorLensQuery = { lens: 'operator' } as const;
}
