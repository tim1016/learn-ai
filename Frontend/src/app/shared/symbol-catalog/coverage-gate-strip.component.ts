import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../pipes/receipt-label.pipe';
import type { CoverageGateState } from './ensure-coverage.service';

/**
 * The picker's gate strip: the one rendering of "this pick is being covered"
 * and "this pick could not be covered". Pure presentation — the state comes
 * from a coverage session and the buttons just report intent.
 *
 * A `view_not_backfillable` failure offers no Retry: retrying cannot derive
 * the view; the operator must switch the picker's adjustment mode.
 */
@Component({
  selector: 'app-coverage-gate-strip',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './coverage-gate-strip.component.html',
  styleUrl: './coverage-gate-strip.component.scss',
})
export class CoverageGateStripComponent {
  readonly state = input.required<CoverageGateState>();

  readonly retry = output();
  readonly cancelled = output();
  readonly dismiss = output();
}
