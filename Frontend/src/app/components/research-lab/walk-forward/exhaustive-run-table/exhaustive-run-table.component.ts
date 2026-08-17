import { DecimalPipe, PercentPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { TableModule } from 'primeng/table';

import type {
  ExhaustiveCandidateResult,
  ExhaustiveRunResponse,
} from '../../../../services/exhaustive-run.types';
import { TimestampDisplayPipe } from '../../../../shared/timestamp';
import { ExhaustiveRunFoldsDialogComponent } from '../exhaustive-run-folds-dialog/exhaustive-run-folds-dialog.component';

@Component({
  selector: 'app-exhaustive-run-table',
  imports: [
    DecimalPipe,
    ExhaustiveRunFoldsDialogComponent,
    PercentPipe,
    TableModule,
    TimestampDisplayPipe,
  ],
  templateUrl: './exhaustive-run-table.component.html',
  styleUrl: './exhaustive-run-table.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExhaustiveRunTableComponent {
  readonly response = input.required<ExhaustiveRunResponse>();
  readonly explainedCandidate = signal<ExhaustiveCandidateResult | null>(null);
  readonly candidates = computed(() => this.response().result.candidates);
  readonly failedCandidates = computed(() =>
    this.candidates().filter((candidate) => candidate.status === 'failed'),
  );

  trackCandidate(_index: number, candidate: ExhaustiveCandidateResult): string {
    return candidate.strategy_spec_hash;
  }
}
