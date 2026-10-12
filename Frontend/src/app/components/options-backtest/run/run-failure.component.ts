import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { RunFailure } from '../options-backtest.types';

/**
 * Why a run failed, as the server says it: a reason that names no date, then
 * each thing that failed. A session is named by its date, in New York dates,
 * and the run as a whole by words.
 */
@Component({
  selector: 'app-options-backtest-run-failure',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-failure.component.html',
  styleUrl: './run-failure.component.scss',
})
export class RunFailureComponent {
  readonly failure = input.required<RunFailure>();
}
