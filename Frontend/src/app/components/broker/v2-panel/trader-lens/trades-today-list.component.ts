import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { ChartFillMarker } from '../lib/broker-v2-panel.types';

/**
 * Fills list for today's session (spec §6, §10).
 *
 * Renders raw fill events from the live chart response.  P&L totals
 * (realized_pnl_today, open_pnl) come from the backend panel projection —
 * this component never computes P&L. Python's FIFO engine is canonical.
 */
@Component({
  selector: 'app-trades-today-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './trades-today-list.component.html',
  styleUrl: './trades-today-list.component.scss',
})
export class TradesTodayListComponent {
  readonly fills = input<readonly ChartFillMarker[]>([]);
  /** fee_fidelity from PanelProfile — "none" → "Fees not reported". */
  readonly feeFidelity = input<'per_fill' | 'aggregate' | 'none'>('none');
  /** Today trading date as int64 ms UTC for display. */
  readonly tradingDateMs = input<number | null>(null);
  /** Backend-computed FIFO realized P&L for today. */
  readonly realizedPnlToday = input<number>(0);
  /** Backend-computed open P&L (null when incomplete marks). */
  readonly openPnl = input<number | null>(null);

  protected readonly hasFills = computed(() => this.fills().length > 0);

  protected readonly feesLabel = computed(() =>
    this.feeFidelity() === 'none' ? 'Fees not reported' : null,
  );

  protected formatPnl(pnl: number | null): string {
    if (pnl === null) return '—';
    const sign = pnl >= 0 ? '+' : '';
    return `${sign}$${Math.abs(pnl).toFixed(2)}`;
  }

  protected pnlClass(pnl: number | null): string {
    if (pnl === null) return '';
    return pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
  }
}
