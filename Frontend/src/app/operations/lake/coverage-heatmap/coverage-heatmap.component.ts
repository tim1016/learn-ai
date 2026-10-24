import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { ReceiptLabelPipe, formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../../shared/timestamp/timestamp-display';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import {
  COVERAGE_STATUSES,
  coverageGlyph,
  type CoverageBoard,
  type CoverageCell,
  type CoverageRow,
} from '../lib/coverage-board';
import type { CoverageStatus } from '../../../shared/data-lake';

export interface CoverageCellSelection {
  readonly symbol: string;
  readonly cell: CoverageCell;
}

interface LegendEntry {
  readonly status: CoverageStatus;
  readonly glyph: string;
  readonly count: number;
}

/**
 * Symbol × trading-day coverage grid.
 *
 * One cell per session the canonical NYSE calendar returned — weekends and
 * holidays are absent from the endpoint's own day list, so the strip has no
 * gaps to explain and none are invented here. Each cell carries its state as
 * a glyph and in its accessible name, so every state is distinguishable
 * without colour. Every cell can be selected: a session with nothing on disk
 * is as much an answer as one with a receipt.
 */
@Component({
  selector: 'app-coverage-heatmap',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './coverage-heatmap.component.html',
  styleUrl: './coverage-heatmap.component.scss',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
})
export class CoverageHeatmapComponent {
  readonly board = input.required<CoverageBoard>();
  /** The session open in the inspector, so its cell reads as selected. */
  readonly selected = input<CoverageCellSelection | null>(null);

  readonly cellSelected = output<CoverageCellSelection>();

  protected readonly rows = computed(() => this.board().rows);

  protected readonly legend = computed<readonly LegendEntry[]>(() => {
    const rows = this.rows();
    return COVERAGE_STATUSES.map((status) => ({
      status,
      glyph: coverageGlyph(status),
      count: rows.reduce((total, row) => total + row.counts[status], 0),
    }));
  });

  protected glyph(status: CoverageStatus): string {
    return coverageGlyph(status);
  }

  /**
   * The cell's whole story in one accessible name: which symbol, which
   * trading date (date-anchored ET — a viewer-local render drifts a day
   * west of UTC) and which state.
   */
  protected cellLabel(row: CoverageRow, cell: CoverageCell): string {
    const date = formatTimestampDisplay(cell.tradingDateMs, { mode: 'date-et' });
    return `${row.symbol}, ${date}, ${formatReceiptLabel(cell.status)}`;
  }

  protected isSelected(row: CoverageRow, cell: CoverageCell): boolean {
    const selected = this.selected();
    return (
      selected !== null &&
      selected.symbol === row.symbol &&
      selected.cell.tradingDateMs === cell.tradingDateMs
    );
  }

  protected select(row: CoverageRow, cell: CoverageCell): void {
    this.cellSelected.emit({ symbol: row.symbol, cell });
  }
}
