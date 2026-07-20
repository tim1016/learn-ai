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

export type CohortTargetPosture = 'PAPER_EXECUTION' | 'UNSAFE' | 'UNKNOWN';

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
  readonly accountId = input<string | null>(null);
  readonly accountPosture = input<CohortTargetPosture>('UNKNOWN');
  readonly selectedIds = input.required<ReadonlySet<string>>();
  readonly authorize = output<readonly string[]>();
  readonly selectionToggled = output<string>();
  readonly threeBotPresetRequested = output();
  readonly fiveBotPresetRequested = output();
  readonly cancelled = output();

  readonly hardBlockerCount = computed(() =>
    this.candidates().reduce((count, candidate) => count + candidate.blockers.length, 0),
  );
  readonly eligibleCandidates = computed(() =>
    this.candidates().filter((candidate) => this.isEligible(candidate)),
  );
  readonly selectedCandidates = computed(() =>
    this.candidates().filter((candidate) => this.selectedIds().has(candidate.candidate.strategyInstanceId)),
  );
  readonly isBlocked = computed(() =>
    this.loading()
    || this.error() !== null
    || this.selectedCandidates().length < 2
    || this.selectedCandidates().some((candidate) => !this.isEligible(candidate)),
  );

  isEligible(candidate: CohortLaunchPreflightCandidate): boolean {
    return candidate.error === null && candidate.blockers.length === 0;
  }

  isSelected(candidate: CohortLaunchPreflightCandidate): boolean {
    return this.selectedIds().has(candidate.candidate.strategyInstanceId);
  }

  authorizeSelected(): void {
    if (!this.isBlocked()) {
      this.authorize.emit(this.selectedCandidates().map((row) => row.candidate.strategyInstanceId));
    }
  }
}
