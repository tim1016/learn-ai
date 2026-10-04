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
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { formatTimestampDisplay } from '../../shared/timestamp/timestamp-display';
import { ArtifactInspectorDrawerComponent } from './artifact-inspector/artifact-inspector-drawer.component';
import { LakeBackfillPanelComponent } from './backfill-panel/lake-backfill-panel.component';
import {
  CoverageHeatmapComponent,
  type CoverageCellSelection,
} from './coverage-heatmap/coverage-heatmap.component';
import {
  ReadinessBannerComponent,
  type RebuildProgress,
} from './readiness-banner/readiness-banner.component';
import { SessionInspectorComponent } from './session-inspector/session-inspector.component';
import { SessionNeedsComponent } from './session-needs/session-needs.component';
import { LakePanelComponent } from './lake-panel/lake-panel.component';
import { LakeStorageFoldComponent } from './storage-summary/lake-storage-fold.component';
import { SymbolReadinessTableComponent } from './symbol-readiness-table/symbol-readiness-table.component';
import { buildCoverageBoard, type CoverageBoard } from './lib/coverage-board';
import type { BackfillPhase } from './lib/data-lake-backfill.store';
import {
  PRICE_ADJUSTMENT_OPTIONS,
  coverageWindow,
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

interface CoverageParams {
  readonly row: SymbolReadinessRow;
  readonly mode: PriceAdjustmentMode;
  readonly bars: DataLakeDataType;
  readonly capDays: number;
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
    ReceiptLabelPipe,
    AssetIdentityComponent,
    ArtifactInspectorDrawerComponent,
    CoverageHeatmapComponent,
    LakeBackfillPanelComponent,
    LakePanelComponent,
    LakeStorageFoldComponent,
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

  /** Split-adjusted first: it is the view every sweep and Golden Search reads. */
  protected readonly mode = signal<PriceAdjustmentMode>('polygon_split_adjusted');
  protected readonly bars = signal<DataLakeDataType>('trade');

  /** The backfill form's starting window; a rebuild replaces it with the symbol's own. */
  protected readonly seedStart = tradingDateAt(Date.now() - DEFAULT_LOOKBACK_DAYS * DAY_MS);
  protected readonly seedEnd = tradingDateAt(Date.now());

  protected readonly readiness = resource({
    params: () => this.mode(),
    loader: ({ params }) => this.lake.runReadiness(params),
  });

  protected readonly storage = resource({
    loader: () => this.lake.storageSummary(),
  });

  protected readonly defaults = resource({
    loader: () => this.lake.backfillDefaults(),
  });

  protected readonly rows = computed(() => {
    const read = this.readiness.value();
    return read?.kind === 'ok' ? readinessRows(read.value.symbols) : [];
  });

  protected readonly totals = computed(() => readinessTotals(this.rows()));

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

  protected readonly coverage = resource({
    params: (): CoverageParams | undefined => {
      const row = this.selectedRow();
      if (row === null) return undefined;
      return { row, mode: this.mode(), bars: this.bars(), capDays: this.maxTradingRangeDays() };
    },
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
    const row = this.board().rows[0];
    const cell = row?.cells.find((candidate) => candidate.tradingDateMs === dateMs);
    return row === undefined || cell === undefined ? null : { symbol: row.symbol, cell };
  });

  protected readonly receipt = signal<CoverageCellSelection | null>(null);

  protected readonly backfillDefaults = computed(() => {
    const read = this.defaults.value();
    return read?.kind === 'ok' ? read.value : null;
  });

  protected readonly storageSummary = computed(() => {
    const read = this.storage.value();
    return read?.kind === 'ok' ? read.value : null;
  });

  protected readonly maxTradingRangeDays = computed(
    () => this.backfillDefaults()?.max_trading_range_days ?? MAX_TRADING_RANGE_DAYS,
  );

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

  /** The symbols a "rebuild all" still has to run, after the one running now. */
  private readonly rebuildQueue = signal<readonly RebuildRequest[]>([]);
  protected readonly rebuildProgress = signal<RebuildProgress | null>(null);

  protected onBars(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.bars.set(value === 'quote' ? 'quote' : 'trade');
  }

  protected selectSession(selection: CoverageCellSelection): void {
    this.selectedDateMs.set(selection.cell.tradingDateMs);
  }

  protected async rebuildAll(): Promise<void> {
    await this.rebuild(rebuildRequests(this.rows()));
  }

  /** Runs the requests one after another: each symbol has its own window, so each is its own backfill. */
  protected async rebuild(requests: readonly RebuildRequest[]): Promise<void> {
    if (requests.length === 0 || this.backfillBusy()) return;
    this.rebuildQueue.set(requests.slice(1));
    await this.startRebuild(requests[0], 1, requests.length);
  }

  /**
   * A finished backfill changes what is on disk, so readiness and storage
   * re-read (coverage follows readiness). A run that failed or was cancelled
   * ends a "rebuild all" too: whatever stopped one symbol would stop the next.
   */
  protected async onRunFinished(phase: BackfillPhase): Promise<void> {
    this.readiness.reload();
    this.storage.reload();
    const progress = this.rebuildProgress();
    const [next, ...rest] = this.rebuildQueue();
    if (phase !== 'completed' || progress === null || next === undefined) {
      this.endRebuild();
      return;
    }
    this.rebuildQueue.set(rest);
    await this.startRebuild(next, progress.position + 1, progress.total);
  }

  private async startRebuild(request: RebuildRequest, position: number, total: number): Promise<void> {
    this.rebuildProgress.set({ symbol: request.symbol, position, total });
    const started = (await this.backfillPanel()?.rebuild(request)) ?? false;
    // The form refused the run and says why in the backfill panel.
    if (!started) this.endRebuild();
  }

  private endRebuild(): void {
    this.rebuildQueue.set([]);
    this.rebuildProgress.set(null);
  }

  private async loadBoard(params: CoverageParams): Promise<CoverageBoard> {
    const window = coverageWindow(params.row, params.capDays);
    const read = await this.lake.coverage({
      symbol: params.row.symbol,
      startTradingDate: window.from,
      endTradingDate: window.to,
      dataType: params.bars,
      priceAdjustmentMode: params.mode,
    });
    return buildCoverageBoard([{ symbol: params.row.symbol, read }]);
  }
}
