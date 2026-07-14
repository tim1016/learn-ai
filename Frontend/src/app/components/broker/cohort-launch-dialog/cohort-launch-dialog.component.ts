import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import type { OperatorBlocker } from '../../../api/operator-blocker.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { OperatorBlockerListComponent } from '../shared/operator-blocker-list/operator-blocker-list.component';

export interface CohortLaunchCandidate {
  strategyInstanceId: string;
  name: string;
  strategyKey: string;
}

export interface CohortLaunchPreflightCandidate {
  candidate: CohortLaunchCandidate;
  blockers: readonly OperatorBlocker[];
  error: string | null;
}

@Component({
  selector: 'app-cohort-launch-dialog',
  imports: [ButtonModule, OperatorBlockerListComponent, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './cohort-launch-dialog.component.html',
  styleUrl: './cohort-launch-dialog.component.scss',
})
export class CohortLaunchDialogComponent {
  readonly open = input.required<boolean>();
  readonly candidates = input.required<readonly CohortLaunchPreflightCandidate[]>();
  readonly loading = input.required<boolean>();
  readonly error = input<string | null>(null);
  readonly authorize = output();
  readonly cancelled = output();

  readonly hardBlockerCount = computed(() =>
    this.candidates().reduce((count, candidate) => count + candidate.blockers.length, 0),
  );
  readonly isBlocked = computed(() =>
    this.loading()
    || this.error() !== null
    || this.candidates().some((candidate) => candidate.error !== null)
    || this.hardBlockerCount() > 0,
  );
}
