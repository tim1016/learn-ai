import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { SessionDetail } from '../run-detail.types';
import { fillRows, levelRows, markRows } from '../session-rows';
import { InstrumentComponent } from './instrument.component';

/**
 * The chart's lines and marks as text: each level with the two instants it is drawn between, each fill with the price it
 * filled at and the price seen, and each mark. Together with the list of events this holds every fact the chart shows, so
 * no meaning rests on the picture. A leg marked and not sold has its own list: a mark is never listed among the fills.
 */
@Component({
  selector: 'app-options-backtest-session-drawn',
  imports: [AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent, InstrumentComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './session-drawn.component.html',
  styleUrl: './session-tables.scss',
})
export class SessionDrawnComponent {
  readonly session = input.required<SessionDetail>();
  protected readonly levels = computed(() => levelRows(this.session()));
  protected readonly fills = computed(() => fillRows(this.session()));
  protected readonly marks = computed(() => markRows(this.session()));
}
