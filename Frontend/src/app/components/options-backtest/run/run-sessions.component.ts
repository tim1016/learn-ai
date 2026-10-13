import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { RunFigures } from '../options-backtest.types';
import { entryRefusalRows } from '../run-words';

/**
 * How the sessions of a run came out, and what did not trade and why. These
 * are the server's counts, each named for what it counts; a count that falls
 * under several reasons lists each reason with its own count. The refusals of a
 * sale and of its cover read as not recorded on a run saved before they were
 * counted (#3109).
 */
@Component({
  selector: 'app-options-backtest-run-sessions',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-sessions.component.html',
  styleUrl: './run-sessions.component.scss',
})
export class RunSessionsComponent {
  readonly figures = input.required<RunFigures>();
  protected readonly refusals = computed(() => entryRefusalRows(this.figures().entries_refused_by_reason));
}
