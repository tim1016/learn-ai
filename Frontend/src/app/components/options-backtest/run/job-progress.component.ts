import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { ProgressView } from '../run-words';

/**
 * Where a run is: the phase in words, the sessions done in the phases that
 * count them (said as the job's unit word, a label, and the two numbers), the
 * latest line of the job's log, and Cancel. Only the phase is announced to a
 * screen reader; a count that moves with every session is not.
 */
@Component({
  selector: 'app-options-backtest-job-progress',
  imports: [ButtonModule, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './job-progress.component.html',
  styleUrl: './job-progress.component.scss',
})
export class JobProgressComponent {
  readonly view = input.required<ProgressView>();
  /** A cancel has been asked for and the job has not yet said it stopped. */
  readonly cancelling = input(false);
  /** Why the cancel could not be asked for. */
  readonly cancelProblem = input<string | null>(null);
  readonly cancelRequested = output();
}
