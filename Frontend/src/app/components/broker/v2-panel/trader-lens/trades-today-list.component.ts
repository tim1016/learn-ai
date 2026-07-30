import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { ChartFillMarker } from '../lib/broker-v2-panel.types';

/** One paired entry/exit trade row. */
export interface TradePair {
  readonly entryFill: ChartFillMarker;
  readonly exitFill: ChartFillMarker | null;
  readonly quantity: number;
  readonly realizedPnl: number | null;
  readonly holdDurationMs: number | null;
}

/**
 * Pair fills into entry/exit trades using a simple FIFO stack.
 *
 * Fills arrive sorted by `filled_at_ms` ascending (server-side guarantee).
 * An unmatched buy with no subsequent sell = open position (exit = null).
 */
export function pairFills(fills: readonly ChartFillMarker[]): TradePair[] {
  const pairs: TradePair[] = [];
  const openBuys: ChartFillMarker[] = [];

  for (const fill of fills) {
    if (fill.side === 'buy') {
      openBuys.push(fill);
    } else {
      const match = openBuys.shift() ?? null;
      if (match) {
        pairs.push({
          entryFill: match,
          exitFill: fill,
          quantity: Math.min(match.quantity, fill.quantity),
          realizedPnl: (fill.price - match.price) * Math.min(match.quantity, fill.quantity),
          holdDurationMs: fill.filled_at_ms - match.filled_at_ms,
        });
      } else {
        // Orphaned sell (short entry or cross — treat as entry with null exit)
        pairs.push({
          entryFill: fill,
          exitFill: null,
          quantity: fill.quantity,
          realizedPnl: null,
          holdDurationMs: null,
        });
      }
    }
  }

  // Remaining open buys = open positions
  for (const buy of openBuys) {
    pairs.push({
      entryFill: buy,
      exitFill: null,
      quantity: buy.quantity,
      realizedPnl: null,
      holdDurationMs: null,
    });
  }

  return pairs;
}

function formatDuration(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${totalSec}s`;
}

/**
 * Trades-today list (spec §6, §10).
 *
 * Pairs fills into entry/exit rows. Realized and open P&L are separate.
 * Fees render as "Fees not reported" when the broker does not supply them
 * (fee_fidelity = "none") — never $0.00.
 *
 * Fee fidelity is a display-layer concern owned by `feeFidelity` input;
 * it is not modelled on TradePair (which is a pure FIFO data model).
 */
@Component({
  selector: 'app-trades-today-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './trades-today-list.component.html',
  styleUrl: './trades-today-list.component.scss',
})
export class TradesTodayListComponent {
  // ── Inputs ────────────────────────────────────────────────────────────────

  readonly fills = input<readonly ChartFillMarker[]>([]);
  /** fee_fidelity from PanelProfile — "none" → "Fees not reported". */
  readonly feeFidelity = input<'per_fill' | 'aggregate' | 'none'>('none');
  /** Bot's RTH/extended-hours policy label (backend-authored). */
  readonly sessionPolicy = input<string>('');
  /** "Today" trading date as int64 ms UTC for display. */
  readonly tradingDateMs = input<number | null>(null);

  // ── Derived ───────────────────────────────────────────────────────────────

  protected readonly pairs = computed(() => pairFills(this.fills()));

  protected readonly realizedTotal = computed(() =>
    this.pairs().reduce(
      (sum, p) => (p.realizedPnl !== null ? sum + p.realizedPnl : sum),
      0,
    ),
  );

  protected readonly hasPairs = computed(() => this.pairs().length > 0);

  protected readonly feesLabel = computed(() =>
    this.feeFidelity() === 'none' ? 'Fees not reported' : null,
  );

  // ── Template helpers ──────────────────────────────────────────────────────

  protected pairId(pair: TradePair): string {
    return `${pair.entryFill.order_ref}-${pair.exitFill?.order_ref ?? 'open'}`;
  }

  protected formatDuration(ms: number | null): string {
    if (ms === null) return '—';
    return formatDuration(ms);
  }

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
