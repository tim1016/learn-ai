import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { RunFigures } from '../options-backtest.types';

/**
 * How the sessions of a run came out, and what did not trade and why. These
 * are the server's counts, each named for what it counts; a count that falls
 * under several reasons lists each reason with its own count.
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
}
