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
 * The latest run's fills (#2794 R8), dated by the run, never "today".
 *
 * The backend sends the run's newest fills and counts them all; when it sent
 * fewer than the run has, the full list says so. Renders raw fill events;
 * P&L stays in the money panel, where the backend's totals are prominent.
 */
@Component({
  selector: 'app-run-fills-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, FillTableComponent, TimestampDisplayComponent],
  templateUrl: './run-fills-list.component.html',
  styleUrl: './run-fills-list.component.scss',
})
export class RunFillsListComponent {
  /** The run's newest fills, oldest first. */
  readonly fills = input<readonly ChartFillMarker[]>([]);
  /** Every fill the run has, as the backend counts its trades. */
  readonly fillCount = input(0);
  /** When the run started, int64 ms UTC; the list is dated by it. */
  readonly runStartedAtMs = input<number | null>(null);

  protected readonly hasFills = computed(() => this.fills().length > 0);
  protected readonly inlineFills = computed(() => this.fills().slice(-INLINE_FILL_LIMIT));
  protected readonly hasMoreFills = computed(() => this.fillCount() > INLINE_FILL_LIMIT);
  protected readonly shownWords = computed(() =>
    this.fillCount() > this.fills().length
      ? `the newest ${this.fills().length} of ${this.fillCount()} fills`
      : `all ${this.fills().length} fills`);
  protected readonly allFillsOpen = signal(false);

  protected openAllFills(): void {
    this.allFillsOpen.set(true);
  }

  protected closeAllFills(): void {
    this.allFillsOpen.set(false);
  }
}
