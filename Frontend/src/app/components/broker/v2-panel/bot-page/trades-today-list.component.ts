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
 * A fills list: today's session, or the bot's run (#2794 R8), labelled by
 * its date and never "today" for a run.
 *
 * Renders raw fill events. P&L stays in the money panel, where the
 * backend-provided totals are prominent.
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
  /** The list's name: "Fills today", or "Fills this run" for a run. */
  readonly heading = input('Fills today');
  /** Backend count; null means the active custody fold cannot provide history. */
  readonly fillCount = input<number | null>(0);
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
      return this.heading() === 'Fills today' ? 'No fills today.' : `No ${this.heading().toLowerCase()}.`;
    }
    return 'Fill details are outside the current chart window.';
  });

  protected openAllFills(): void {
    this.allFillsOpen.set(true);
  }

  protected closeAllFills(): void {
    this.allFillsOpen.set(false);
  }
}
