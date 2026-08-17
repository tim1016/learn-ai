import { DecimalPipe, PercentPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';
import { DialogModule } from 'primeng/dialog';
import { TableModule } from 'primeng/table';

import type { ExhaustiveCandidateResult } from '../../../../services/exhaustive-run.types';
import { TimestampDisplayPipe } from '../../../../shared/timestamp';

@Component({
  selector: 'app-exhaustive-run-folds-dialog',
  imports: [DecimalPipe, DialogModule, PercentPipe, RouterLink, TableModule, TimestampDisplayPipe],
  templateUrl: './exhaustive-run-folds-dialog.component.html',
  styleUrl: './exhaustive-run-folds-dialog.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExhaustiveRunFoldsDialogComponent {
  readonly candidate = input<ExhaustiveCandidateResult | null>(null);
  readonly closed = output();

  onVisibleChange(visible: boolean): void {
    if (!visible) this.closed.emit();
  }
}
