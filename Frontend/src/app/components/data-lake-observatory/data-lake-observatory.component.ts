import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { formatTimestampDisplay } from '../../shared/timestamp/timestamp-display';
import { ArtifactInspectorDrawerComponent } from './artifact-inspector/artifact-inspector-drawer.component';
import { LakeBackfillPanelComponent } from './backfill-panel/lake-backfill-panel.component';
import {
  CoverageHeatmapComponent,
  type CoverageCellSelection,
} from './coverage-heatmap/coverage-heatmap.component';
import { GapBannerComponent } from './gap-banner/gap-banner.component';
import { LakePanelComponent } from './lake-panel/lake-panel.component';
import { LakeToggleComponent } from './lake-toggle/lake-toggle.component';
import {
  ReadinessBannerComponent,
  type RebuildProgress,
} from './readiness-banner/readiness-banner.component';
import { SessionInspectorComponent } from './session-inspector/session-inspector.component';
import { SessionNeedsComponent } from './session-needs/session-needs.component';
import { LakeStorageFoldComponent } from './storage-summary/lake-storage-fold.component';
import { SymbolReadinessTableComponent } from './symbol-readiness-table/symbol-readiness-table.component';
import { buildCoverageBoard, type CoverageBoard } from './lib/coverage-board';
import {
  PRICE_ADJUSTMENT_OPTIONS,
  coverageWindow,
  fillRows,
  readinessRows,
  readinessTotals,
  rebuildRequests,
  type RebuildRequest,
  type SymbolReadinessRow,
} from './lib/lake-readiness';
import {
  DataLakeService,
  MAX_TRADING_RANGE_DAYS,
  describeFailure,
  type DataLakeDataType,
  type PriceAdjustmentMode,
} from '../../shared/data-lake';

const DAY_MS = 86_400_000;
const DEFAULT_LOOKBACK_DAYS = 30;
const BAR_TYPE_OPTIONS: readonly DataLakeDataType[] = ['trade', 'quote'];

const EMPTY_BOARD: CoverageBoard = {
  rows: [],
  problems: [],
  sessionCount: 0,
  firstSessionMs: null,
  lastSessionMs: null,
};

/** Which banner started a list of backfills: each narrates only its own. */
type BackfillList = 'rebuild' | 'fill';

/** What the session grid reads. Compared by value, so a re-read of readiness alone does not reset the grid. */
interface CoverageRequest {
  readonly symbol: string;
  readonly from: string;
  readonly to: string;
  readonly mode: PriceAdjustmentMode;
  readonly bars: DataLakeDataType;
}

function sameCoverageRequest(
  a: CoverageRequest | undefined,
  b: CoverageRequest | undefined,
): boolean {
  if (a === undefined || b === undefined) return a === b;
  return (
    a.symbol === b.symbol &&
    a.from === b.from &&
    a.to === b.to &&
    a.mode === b.mode &&
    a.bars === b.bars
  );
}

/**
 * A trading date is a date-anchored value: resolving "today" in the
 * viewer's own zone would name tomorrow's session for anyone east of ET and
 * yesterday's for anyone far enough west. The shared display module's ET
 * date mode is the one place that resolution is allowed to happen.
 */
function tradingDateAt(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'date-et' });
}

/**
 * What the lake holds and whether a run can read it.
 *
 * The page answers in the order an operator needs: is anything on disk
 * unreadable, what a session needs to be readable, which symbols are
 * affected, and the backfill that fixes them. The catalog alone cannot
 * answer the first question — it calls a split-adjusted session complete
 * once its price file is committed, while a run also refuses a file without
 * a current adjustment record — so readiness is read from the data plane,
 * never inferred here from catalog status.
 */
@Component({
  selector: 'app-data-lake-observatory',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './data-lake-observatory.component.html',
  styleUrl: './data-lake-observatory.component.scss',
  imports: [
    PageHeaderComponent,
    AssetIdentityComponent,
    ArtifactInspectorDrawerComponent,
    CoverageHeatmapComponent,
    GapBannerComponent,
    LakeBackfillPanelComponent,
    LakePanelComponent,
    LakeStorageFoldComponent,
    LakeToggleComponent,
    ReadinessBannerComponent,
    SessionInspectorComponent,
    SessionNeedsComponent,
    SymbolReadinessTableComponent,
  ],
})
export class DataLakeObservatoryComponent {
  private readonly lake = inject(DataLakeService);
  private readonly backfillPanel = viewChild(LakeBackfillPanelComponent);

  protected readonly modeOptions = PRICE_ADJUSTMENT_OPTIONS;
  protected readonly barTypeOptions = BAR_TYPE_OPTIONS;

  /**
   * The view the whole page reads: readiness, the session grid and the tree a
   * backfill writes. Split-adjusted trade bars first: they are what every
   * sweep and Golden Search reads.
   */
  protected readonly mode = signal<PriceAdjustmentMode>('polygon_split_adjusted');
  protected readonly bars = signal<DataLakeDataType>('trade');

  /** The backfill form's starting window; a rebuild replaces it with the symbol's own. */
  protected readonly seedStart = tradingDateAt(Date.now() - DEFAULT_LOOKBACK_DAYS * DAY_MS);
  protected readonly seedEnd = tradingDateAt(Date.now());

  protected readonly readiness = resource({
    params: () => ({ mode: this.mode(), bars: this.bars() }),
    loader: ({ params }) => this.lake.runReadiness(params.mode, params.bars),
  });

  protected readonly storage = resource({
    loader: () => this.lake.storageSummary(),
  });

  protected readonly defaults = resource({
    loader: () => this.lake.backfillDefaults(),
  });

  protected readonly rows = computed(() => {
    const read = this.readiness.value();
    return read?.kind === 'ok' ? readinessRows(read.value.symbols, this.bars()) : [];
  });

  protected readonly totals = computed(() => readinessTotals(this.rows()));
  /** The symbols with sessions missing between their first and last that a backfill can fetch. */
  protected readonly gapRows = computed(() => fillRows(this.rows()));

  /** Follows the rows: the pick survives a re-read, and the first row stands in when it is gone. */
  protected readonly selectedSymbol = linkedSignal<readonly SymbolReadinessRow[], string | null>({
    source: this.rows,
    computation: (rows, previous) => {
      const kept = previous?.value ?? null;
      if (kept !== null && rows.some((row) => row.symbol === kept)) return kept;
      return rows[0]?.symbol ?? null;
    },
  });

  protected readonly selectedRow = computed(
    () => this.rows().find((row) => row.symbol === this.selectedSymbol()) ?? null,
  );

  protected readonly backfillDefaults = computed(() => {
    const read = this.defaults.value();
    return read?.kind === 'ok' ? read.value : null;
  });

  protected readonly maxTradingRangeDays = computed(
    () => this.backfillDefaults()?.max_trading_range_days ?? MAX_TRADING_RANGE_DAYS,
  );

  private readonly coverageRequest = computed<CoverageRequest | undefined>(
    () => {
      const row = this.selectedRow();
      if (row === null) return undefined;
      const window = coverageWindow(row, this.maxTradingRangeDays());
      return {
        symbol: row.symbol,
        from: window.from,
        to: window.to,
        mode: this.mode(),
        bars: this.bars(),
      };
    },
    { equal: sameCoverageRequest },
  );

  protected readonly coverage = resource({
    params: () => this.coverageRequest(),
    loader: ({ params }) => this.loadBoard(params),
  });

  protected readonly board = computed(() => this.coverage.value() ?? EMPTY_BOARD);

  /** The picked session's date; a different symbol, view or bar type clears it. */
  private readonly selectedDateMs = linkedSignal<unknown, number | null>({
    source: () => [this.selectedSymbol(), this.mode(), this.bars()],
    computation: () => null,
  });

  /** Read off the board, so a re-read after a backfill shows the session as it now stands. */
  protected readonly selection = computed<CoverageCellSelection | null>(() => {
    const dateMs = this.selectedDateMs();
    const row = this.board().rows.at(0);
    const cell = row?.cells.find((candidate) => candidate.tradingDateMs === dateMs);
    return row === undefined || cell === undefined ? null : { symbol: row.symbol, cell };
  });

  protected readonly receipt = signal<CoverageCellSelection | null>(null);

  protected readonly storageSummary = computed(() => {
    const read = this.storage.value();
    return read?.kind === 'ok' ? read.value : null;
  });

  protected readonly readinessProblem = computed(() => describeFailure(this.readiness.value()));

  protected readonly symbolsPending = computed(() =>
    this.readiness.isLoading() && this.rows().length === 0 ? 'Reading the catalog…' : null,
  );

  /** The coverage read's own refusal for the selected symbol, when it sent one. */
  protected readonly sessionsProblem = computed(() => this.board().problems.at(0) ?? null);

  /** What the sessions panel says in place of a grid, or null when it has one to draw. */
  protected readonly sessionsPending = computed(() => {
    if (this.board().sessionCount > 0) return null;
    if (this.coverage.isLoading() || this.symbolsPending() !== null) return 'Walking the calendar…';
    if (this.selectedSymbol() === null) return 'The lake holds no symbol in this view to show sessions for.';
    return 'The exchange calendar has no sessions in this window.';
  });

  protected readonly storageProblem = computed(() => describeFailure(this.storage.value()));

  protected readonly backfillBusy = computed(() => this.backfillPanel()?.busy() ?? false);

  /** The symbol a list is on, its place in it, and which banner started it; null when none is running. */
  private readonly rebuildProgress = signal<(RebuildProgress & { readonly list: BackfillList }) | null>(null);

  /** What each banner narrates: its own list of symbols. A single symbol's run is the backfill panel's to show. */
  protected readonly queueProgress = computed(() => this.listProgress('rebuild'));
  protected readonly fillProgress = computed(() => this.listProgress('fill'));

  protected selectSession(selection: CoverageCellSelection): void {
    this.selectedDateMs.set(selection.cell.tradingDateMs);
  }

  protected async rebuildAll(): Promise<void> {
    await this.rebuild(rebuildRequests(this.rows()));
  }

  /**
   * Runs the requests one after another: each symbol has its own window, so
   * each is its own backfill.
   *
   * The list ends at the first run that does not complete — whatever stopped
   * one symbol would stop the next — and when the operator moves to another
   * view, because the backfill panel writes the view on screen and the rest
   * of the list was composed for the one they left.
   */
  protected async rebuild(requests: readonly RebuildRequest[], list: BackfillList = 'rebuild'): Promise<void> {
    const panel = this.backfillPanel();
    if (panel === undefined || this.backfillBusy() || this.rebuildProgress() !== null) return;
    const mode = this.mode();
    const bars = this.bars();
    try {
      for (const [index, request] of requests.entries()) {
        if (this.mode() !== mode || this.bars() !== bars) return;
        this.rebuildProgress.set({
          symbol: request.symbol,
          position: index + 1,
          total: requests.length,
          list,
        });
        const end = await panel.rebuild(request);
        if (end === null) return;
        // The panel signals a finished run only once it goes idle, and between
        // two symbols of a list it never does.
        this.onRunFinished();
        if (end !== 'completed') return;
      }
    } finally {
      this.rebuildProgress.set(null);
    }
  }

  private listProgress(list: BackfillList): RebuildProgress | null {
    const progress = this.rebuildProgress();
    return progress !== null && progress.list === list && progress.total > 1 ? progress : null;
  }

  /** A finished backfill changes what is on disk, so every read of the lake is taken again. */
  protected onRunFinished(): void {
    this.readiness.reload();
    this.coverage.reload();
    this.storage.reload();
  }

  private async loadBoard(request: CoverageRequest): Promise<CoverageBoard> {
    const read = await this.lake.coverage({
      symbol: request.symbol,
      startTradingDate: request.from,
      endTradingDate: request.to,
      dataType: request.bars,
      priceAdjustmentMode: request.mode,
    });
    return buildCoverageBoard([{ symbol: request.symbol, read }]);
  }
}
