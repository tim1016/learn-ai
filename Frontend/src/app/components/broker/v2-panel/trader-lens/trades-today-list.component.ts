import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';
import { Drawer } from 'primeng/drawer';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { ChartFillMarker } from '../lib/broker-v2-panel.types';
import { FillTableComponent } from './fill-table/fill-table.component';

const INLINE_FILL_LIMIT = 4;

/**
 * Fills list for today's session (spec §6, §10).
 *
 * Renders raw fill events from the live chart response. P&L stays in the
 * adjacent trader summary, where the backend-provided totals are prominent.
 */
@Component({
  selector: 'app-trades-today-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, FillTableComponent, TimestampDisplayComponent],
  templateUrl: './trades-today-list.component.html',
  styleUrl: './trades-today-list.component.scss',
})
export class TradesTodayListComponent {
  readonly fills = input<readonly ChartFillMarker[]>([]);
  /** Backend count; null means the active custody fold cannot provide history. */
  readonly fillCount = input<number | null>(0);
  /** fee_fidelity from PanelProfile — "none" → "Fees not reported". */
  readonly feeFidelity = input<'per_fill' | 'aggregate' | 'none'>('none');
  /** Today trading date as int64 ms UTC for display. */
  readonly tradingDateMs = input<number | null>(null);

  protected readonly hasFills = computed(() => this.fills().length > 0);
  protected readonly inlineFills = computed(() => this.fills().slice(-INLINE_FILL_LIMIT));
  protected readonly hasMoreFills = computed(() => this.fills().length > INLINE_FILL_LIMIT);
  protected readonly allFillsOpen = signal(false);

  protected readonly emptyStateLabel = computed(() => {
    const fillCount = this.fillCount();
    if (fillCount === null) {
      return 'Fill history unavailable from active custody folds.';
    }
    if (fillCount === 0) {
      return 'No fills today.';
    }
    return 'Fill details are outside the current chart window.';
  });

  protected readonly feesLabel = computed(() =>
    this.feeFidelity() === 'none' ? 'Fees not reported' : null,
  );

  protected openAllFills(): void {
    this.allFillsOpen.set(true);
  }

  protected closeAllFills(): void {
    this.allFillsOpen.set(false);
  }
}
