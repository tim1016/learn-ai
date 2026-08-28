import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';

import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../shared/timestamp/timestamp-display';
import { ArtifactInspectorDrawerComponent } from './artifact-inspector/artifact-inspector-drawer.component';
import { LakeBackfillPanelComponent } from './backfill-panel/lake-backfill-panel.component';
import {
  CoverageHeatmapComponent,
  type CoverageCellSelection,
} from './coverage-heatmap/coverage-heatmap.component';
import {
  CoverageQueryBarComponent,
  type ObservatoryQuery,
} from './coverage-query-bar/coverage-query-bar.component';
import { LakeStorageSummaryComponent } from './storage-summary/lake-storage-summary.component';
import { buildCoverageBoard, parseSymbols, type CoverageBoard } from './lib/coverage-board';
import { DataLakeService } from './lib/data-lake.service';
import type { DataLakeRead } from './lib/data-lake.types';

const DAY_MS = 86_400_000;
const DEFAULT_LOOKBACK_DAYS = 30;

const EMPTY_BOARD: CoverageBoard = {
  rows: [],
  problems: [],
  notEnabled: false,
  sessionCount: 0,
  firstSessionMs: null,
  lastSessionMs: null,
};

/**
 * A trading date is a date-anchored value: resolving "today" in the
 * viewer's own zone would name tomorrow's session for anyone east of ET and
 * yesterday's for anyone far enough west. The shared display module's ET
 * date mode is the one place that resolution is allowed to happen.
 */
function tradingDateAt(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'date-et' });
}

export function defaultObservatoryQuery(nowMs: number = Date.now()): ObservatoryQuery {
  return {
    symbolsText: '',
    startTradingDate: tradingDateAt(nowMs - DEFAULT_LOOKBACK_DAYS * DAY_MS),
    endTradingDate: tradingDateAt(nowMs),
    dataType: 'trade',
    priceAdjustmentMode: 'raw',
  };
}

/**
 * The operator's first visual answer to "what data do we own?".
 *
 * Every panel here is a projection of the flag-gated data-lake catalog. The
 * lake is dark in production until the enablement slice flips
 * `DATA_LAKE_ENABLED`, and a dark lake answers 404 on every route — so the
 * page names that state outright rather than spinning forever or rendering
 * an empty catalog as if it were a real, empty one.
 */
@Component({
  selector: 'app-data-lake-observatory',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './data-lake-observatory.component.html',
  styleUrl: './data-lake-observatory.component.scss',
  imports: [
    PageHeaderComponent,
    ReceiptLabelPipe,
    ArtifactInspectorDrawerComponent,
    CoverageHeatmapComponent,
    CoverageQueryBarComponent,
    LakeBackfillPanelComponent,
    LakeStorageSummaryComponent,
  ],
})
export class DataLakeObservatoryComponent {
  private readonly lake = inject(DataLakeService);

  protected readonly query = signal<ObservatoryQuery>(defaultObservatoryQuery());
  protected readonly selection = signal<CoverageCellSelection | null>(null);

  protected readonly storage = resource({
    loader: () => this.lake.storageSummary(),
  });

  protected readonly defaults = resource({
    loader: () => this.lake.backfillDefaults(),
  });

  protected readonly coverage = resource({
    params: () => this.query(),
    loader: ({ params }) => this.loadBoard(params),
  });

  protected readonly board = computed(() => this.coverage.value() ?? EMPTY_BOARD);

  protected readonly backfillDefaults = computed(() => {
    const read = this.defaults.value();
    return read?.kind === 'ok' ? read.value : null;
  });

  protected readonly storageSummary = computed(() => {
    const read = this.storage.value();
    return read?.kind === 'ok' ? read.value : null;
  });

  /**
   * True as soon as any of the three reads answers 404. One dark route
   * means the router is not mounted at all, so the whole page is dark.
   */
  protected readonly notEnabled = computed(
    () =>
      this.board().notEnabled ||
      this.storage.value()?.kind === 'not_enabled' ||
      this.defaults.value()?.kind === 'not_enabled',
  );

  protected readonly hasSymbols = computed(
    () => parseSymbols(this.query().symbolsText, this.maxSymbolLength()).symbols.length > 0,
  );

  protected readonly maxSymbolLength = computed(
    () => this.backfillDefaults()?.max_symbol_length ?? 20,
  );

  protected readonly storageProblem = computed(() => describeProblem(this.storage.value()));

  protected apply(next: ObservatoryQuery): void {
    this.selection.set(null);
    this.query.set(next);
  }

  protected openInspector(selection: CoverageCellSelection): void {
    this.selection.set(selection);
  }

  protected closeInspector(): void {
    this.selection.set(null);
  }

  /** A finished backfill changes what is on disk; both projections re-read. */
  protected onRunFinished(): void {
    this.coverage.reload();
    this.storage.reload();
  }

  private async loadBoard(query: ObservatoryQuery): Promise<CoverageBoard> {
    const { symbols } = parseSymbols(query.symbolsText, this.maxSymbolLength());
    if (symbols.length === 0) return EMPTY_BOARD;
    const reads = await Promise.all(
      symbols.map(async (symbol) => ({
        symbol,
        read: await this.lake.coverage({
          symbol,
          startTradingDate: query.startTradingDate,
          endTradingDate: query.endTradingDate,
          dataType: query.dataType,
          priceAdjustmentMode: query.priceAdjustmentMode,
        }),
      })),
    );
    return buildCoverageBoard(reads);
  }
}

function describeProblem(
  read: DataLakeRead<unknown> | undefined,
): { reason: string; message: string } | null {
  if (read === undefined) return null;
  if (read.kind === 'rejected') return { reason: read.reason, message: read.message };
  if (read.kind === 'unavailable') return { reason: 'unavailable', message: read.message };
  return null;
}
