import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { SessionDetail } from '../run-detail.types';
import { eventRows } from '../session-rows';
import { InstrumentComponent } from './instrument.component';

/**
 * What happened on a day, in the order it happened: the time in New York time, what it was, whose it was, the contract,
 * the price, the reason, and the words the server stored for it, exactly as it stored them. This list is the chart's
 * equivalent for those who cannot see it.
 */
@Component({
  selector: 'app-options-backtest-session-events',
  imports: [AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent, InstrumentComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './session-events.component.html',
  styleUrl: './session-tables.scss',
})
export class SessionEventsComponent {
  readonly session = input.required<SessionDetail>();
  protected readonly rows = computed(() => eventRows(this.session()));
}
