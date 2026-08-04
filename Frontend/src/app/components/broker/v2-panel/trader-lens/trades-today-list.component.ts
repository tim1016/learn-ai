import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { fmtCurrency } from '../../format';
import type { ChartFillMarker } from '../lib/broker-v2-panel.types';

/**
 * Fills list for today's session (spec §6, §10).
 *
 * Renders raw fill events from the live chart response. P&L stays in the
 * adjacent trader summary, where the backend-provided totals are prominent.
 */
@Component({
  selector: 'app-trades-today-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './trades-today-list.component.html',
  styleUrl: './trades-today-list.component.scss',
})
export class TradesTodayListComponent {
  readonly fills = input<readonly ChartFillMarker[]>([]);
  /** fee_fidelity from PanelProfile — "none" → "Fees not reported". */
  readonly feeFidelity = input<'per_fill' | 'aggregate' | 'none'>('none');
  /** Today trading date as int64 ms UTC for display. */
  readonly tradingDateMs = input<number | null>(null);

  protected readonly hasFills = computed(() => this.fills().length > 0);

  protected readonly feesLabel = computed(() =>
    this.feeFidelity() === 'none' ? 'Fees not reported' : null,
  );

  protected readonly fmtCurrency = fmtCurrency;
}
