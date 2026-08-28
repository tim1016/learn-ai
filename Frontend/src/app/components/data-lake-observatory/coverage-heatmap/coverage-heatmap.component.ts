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
import type { CoverageStatus } from '../lib/data-lake.types';

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
 * a glyph and in its accessible name, so the four artifact states plus
 * `missing` are distinguishable without colour.
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
  /** Artifact currently open in the inspector, so its cell reads as selected. */
  readonly selectedArtifactId = input<number | null>(null);

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
   * west of UTC), which state, and whether there is a receipt to open.
   */
  protected cellLabel(row: CoverageRow, cell: CoverageCell): string {
    const date = formatTimestampDisplay(cell.tradingDateMs, { mode: 'date-et' });
    const status = formatReceiptLabel(cell.status);
    return cell.artifactId === null
      ? `${row.symbol}, ${date}, ${status}, no artifact receipt`
      : `${row.symbol}, ${date}, ${status}, open artifact receipt`;
  }

  protected select(row: CoverageRow, cell: CoverageCell): void {
    if (cell.artifactId === null) return;
    this.cellSelected.emit({ symbol: row.symbol, cell });
  }
}
