import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { RunSessionService } from '../../../services/run-session.service';
import { PastChainInspectorComponent } from '../past-chain-inspector/past-chain-inspector.component';
import { AssetIdentityComponent } from '../../../shared/asset-identity';

import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import type { DataLabCompanionSettings } from '../data-lab-workspace-store';
import {
  buildDatasetPlanPayload,
  buildGenerateZipPayload,
  type GenerateZipMapperInput,
  type OptionsCompanionWireConfig,
} from '../data-lab-request-mapper';
import { DataLabPlanService, type DataLabPlanReceipt } from './data-lab-plan.service';
import { ExportColumnPickerComponent } from './export-column-picker/export-column-picker.component';
import { columnsForPayload } from './export-csv-options';

/**
 * Data Lab Build dataset (PRD §7.4).
 *
 * NEVER mounts DataLabChartComponent and never calls the chart endpoint
 * (FR-004). The recipe is a progressive four-section form over the shared
 * workspace state; columns/sessions/estimates come from the Python plan
 * receipt rendered verbatim (FR-012); generation goes through
 * `RunSessionService.start()` — the ONLY submission path (FR-005).
 * The plan re-runs by itself whenever the recipe changes, so the
 * dataset.csv column picker always lists the current columns (owner
 * decision 2026-09-19).
 */
@Component({
  selector: 'app-data-lab-export',
  imports: [
    RouterLink,
    PastChainInspectorComponent,
    AssetIdentityComponent,
    ExportColumnPickerComponent,
  ],
  templateUrl: './export.component.html',
  styleUrls: ['./export.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExportComponent {
  readonly store = inject(DataLabWorkspaceStore);
  private readonly planService = inject(DataLabPlanService);
  readonly runSession = inject(RunSessionService);

  // ── Fixed flags the monolith carried (not scope; kept local) ──
  readonly warmup = signal(true);
  readonly adjustForDividends = signal(false);
  readonly sort = signal<'asc' | 'desc'>('asc');
  readonly polygonLimit = signal(50000);

  // ── Options companion config (feeds the mapper's options block) ──
  readonly optionsStrikesEachSide = signal(3);
  readonly optionsIncludeCalls = signal(true);
  readonly optionsIncludePuts = signal(true);
  readonly optionsDteDistance = signal(0);
  readonly optIncludeOhlcv = signal(true);
  readonly optIncludeVwap = signal(true);
  readonly optIncludeTransactions = signal(true);
  readonly optIncludeOi = signal(false);
  readonly optIncludeIv = signal(true);
  readonly optIncludeDelta = signal(true);
  readonly optIncludeGamma = signal(true);
  readonly optIncludeTheta = signal(true);
  readonly optIncludeVega = signal(true);
  readonly optIncludeRho = signal(false);
  readonly optIncludeDiscontinuity = signal(true);
  readonly optRiskFreeRate = signal(0.05);
  readonly optDividendYield = signal(0.0);

  // ── Plan receipt ──────────────────────────────────────────
  readonly planLoading = signal(false);
  readonly planError = signal('');
  readonly planReceipt = computed<DataLabPlanReceipt | null>(() =>
    this.store.datasetPlanReceipt() as DataLabPlanReceipt | null,
  );
  /** Only the newest plan request may write the receipt. */
  private planRequestSeq = 0;

  /** Serialized plan request for the live recipe; null until a scope is
   *  committed. The receipt's stored signature is compared against it. */
  private readonly planSignature = computed(() => {
    const input = this.recipeInput();
    return input ? JSON.stringify(buildDatasetPlanPayload(input)) : null;
  });

  /** The stored receipt describes a recipe that no longer matches the live
   *  workspace (ticker/window/timeframe/indicators/options changed after the
   *  plan ran). Its counts must not read as current in §4 — the template
   *  labels the receipt stale until the automatic re-plan lands. */
  readonly planReceiptStale = computed(() => {
    if (!this.planReceipt()) return false;
    const stored = this.store.datasetPlanReceiptSignature();
    return stored !== null && stored !== this.planSignature();
  });

  /** The `columns` the generate payload carries — the owner's ticks,
   *  narrowed to the columns the current plan lists. */
  readonly payloadColumns = computed(() =>
    columnsForPayload(this.store.exportColumns(), this.planReceipt()?.output_columns ?? null),
  );

  readonly generateStarting = signal(false);
  readonly generateError = signal('');

  /** A run is active (fetching or bundling) — a second generate is
   *  disabled meanwhile (PRD §16); cancellation stays available via the
   *  shell-owned RunDock across routes. */
  readonly runActive = computed(
    () =>
      this.runSession.state() === 'fetching' ||
      this.runSession.state() === 'bundling' ||
      this.generateStarting(),
  );

  /** Generation waits until the receipt is current (in flight or failed
   *  re-plan): the column selection is narrowed against the plan, so it
   *  must describe the live recipe. */
  readonly generateBlocked = computed(
    () => this.runActive() || this.planLoading() || this.planReceiptStale(),
  );

  readonly generateLabel = computed(() => {
    if (this.runActive()) return 'Run in progress…';
    if (this.planLoading()) return 'Updating column list…';
    return 'Generate dataset ZIP';
  });

  readonly timeColumnsSummary = computed(() => {
    const timeColumn = this.planReceipt()?.time_column;
    return timeColumn ? `unix_ts, ${timeColumn}` : 'unix_ts';
  });

  readonly dataColumnsSummary = computed(() => {
    const available = this.planReceipt()?.output_columns;
    if (!available) return 'waiting for the plan';
    const selected = this.payloadColumns();
    return selected === null
      ? `all ${available.length}`
      : `${selected.length} of ${available.length}`;
  });

  constructor() {
    // Keep the column list current by itself: re-plan whenever the plan
    // request for the live recipe differs from the one the receipt answers.
    effect(() => {
      const signature = this.planSignature();
      if (signature === null || signature === this.store.datasetPlanReceiptSignature()) return;
      untracked(() => void this.loadPlan());
    });
  }

  readonly scopeSummary = computed(() => {
    const ticker = this.store.committedTicker();
    const window = this.store.committedWindow();
    if (!ticker || !window) return 'Commit a scope first (Explore or the shell scope bar).';
    const d = this.store.draft();
    return `${ticker} · ${d.multiplier} ${d.timespan} bars · ${d.session} session`;
  });

  readonly indicatorSummary = computed(() => {
    const count = this.store.indicators().length;
    return count === 1 ? '1 indicator instance' : `${count} indicator instances`;
  });

  private buildOptionsConfig(): OptionsCompanionWireConfig {
    return {
      enabled: this.store.companions().optionsCompanionEnabled,
      strikes_each_side: this.optionsStrikesEachSide(),
      include_calls: this.optionsIncludeCalls(),
      include_puts: this.optionsIncludePuts(),
      dte_distance: this.optionsDteDistance(),
      include_ohlcv: this.optIncludeOhlcv(),
      include_vwap: this.optIncludeVwap(),
      include_transactions: this.optIncludeTransactions(),
      include_open_interest: this.optIncludeOi(),
      include_iv: this.optIncludeIv(),
      include_delta: this.optIncludeDelta(),
      include_gamma: this.optIncludeGamma(),
      include_theta: this.optIncludeTheta(),
      include_vega: this.optIncludeVega(),
      include_rho: this.optIncludeRho(),
      include_discontinuity: this.optIncludeDiscontinuity(),
      risk_free_rate: this.optRiskFreeRate(),
      dividend_yield: this.optDividendYield(),
    };
  }

  /** One mapper input — the plan and generate payloads both derive from it. */
  private recipeInput(): Omit<GenerateZipMapperInput, 'columns'> | null {
    const ticker = this.store.committedTicker();
    const window = this.store.committedWindow();
    if (!ticker || !window) return null;
    const draft = this.store.draft();
    return {
      ticker,
      window,
      indicators: this.store.indicators(),
      session: draft.session,
      forwardFill: draft.forwardFill,
      adjusted: draft.adjusted,
      companions: this.store.companions(),
      options: this.buildOptionsConfig(),
      warmup: this.warmup(),
      adjustForDividends: this.adjustForDividends(),
      timespan: draft.timespan,
      multiplier: draft.multiplier,
      sort: this.sort(),
      limit: this.polygonLimit(),
      timeZone: this.store.exportTimeZone(),
    };
  }

  async loadPlan(): Promise<void> {
    const input = this.recipeInput();
    if (!input) {
      this.planError.set('Commit a ticker and window before previewing columns.');
      return;
    }
    const payload = buildDatasetPlanPayload(input);
    const seq = ++this.planRequestSeq;
    this.planLoading.set(true);
    this.planError.set('');
    try {
      const receipt = await this.planService.plan(payload);
      if (seq !== this.planRequestSeq) return;
      this.store.setDatasetPlanReceipt(
        receipt as Record<string, unknown>,
        JSON.stringify(payload),
      );
    } catch (e: unknown) {
      if (seq !== this.planRequestSeq) return;
      this.planError.set(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === this.planRequestSeq) this.planLoading.set(false);
    }
  }

  /** The ONLY submission path (FR-005). */
  async generate(): Promise<void> {
    if (this.generateBlocked()) return;
    const input = this.recipeInput();
    const payload = input ? buildGenerateZipPayload({ ...input, columns: this.payloadColumns() }) : null;
    if (!payload) {
      this.generateError.set('Commit a ticker and window before generating.');
      return;
    }
    this.generateError.set('');
    this.generateStarting.set(true);
    try {
      await this.runSession.start(payload);
      const sessionId = this.runSession.sessionId();
      if (sessionId) {
        this.store.setGenerationRun({ id: sessionId, status: this.runSession.state() });
      }
    } catch (e: unknown) {
      this.generateError.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.generateStarting.set(false);
    }
  }

  // ── Toggle helpers (value/change wiring without template forms) ──
  onCompanionToggle(key: keyof DataLabCompanionSettings, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.store.patchCompanions({ [key]: checked } as Partial<DataLabCompanionSettings>);
  }

  onWarmupToggle(event: Event): void {
    this.warmup.set((event.target as HTMLInputElement).checked);
  }

  onAdjustDividendsToggle(event: Event): void {
    this.adjustForDividends.set((event.target as HTMLInputElement).checked);
  }

  onSortChange(event: Event): void {
    this.sort.set((event.target as HTMLSelectElement).value === 'desc' ? 'desc' : 'asc');
  }

  onLimitInput(event: Event): void {
    const value = Number((event.target as HTMLInputElement).value);
    if (Number.isFinite(value) && value > 0) this.polygonLimit.set(Math.floor(value));
  }

  onStrikesInput(event: Event): void {
    const value = Number((event.target as HTMLInputElement).value);
    if (Number.isFinite(value)) this.optionsStrikesEachSide.set(Math.max(1, Math.min(25, value)));
  }

  onDteInput(event: Event): void {
    const value = Number((event.target as HTMLInputElement).value);
    if (Number.isFinite(value) && value >= 0) this.optionsDteDistance.set(Math.floor(value));
  }

  onRateInput(target: 'riskFree' | 'dividendYield', event: Event): void {
    const value = Number((event.target as HTMLInputElement).value);
    if (!Number.isFinite(value)) return;
    if (target === 'riskFree') this.optRiskFreeRate.set(value);
    else this.optDividendYield.set(value);
  }

  onOptionsFlagToggle(key: OptionsFlagKey, event: Event): void {
    this[key].set((event.target as HTMLInputElement).checked);
  }

  // ── Template helpers ──────────────────────────────────────
  /** Render session entries defensively: the current contract ships
   *  YYYY-MM-DD strings in `exchange_sessions`; the server is concurrently
   *  moving to ms-UTC session anchors, so object entries with
   *  open/close anchors render as UTC instants instead of breaking. */
  sessionLabels(receipt: DataLabPlanReceipt): string[] {
    const sessions = receipt.exchange_sessions ?? [];
    return sessions.map((entry): string => {
      if (typeof entry === 'string') return entry;
      if (typeof entry === 'object' && entry !== null) {
        const e = entry as { open_ms_utc?: unknown; close_ms_utc?: unknown };
        const open = typeof e.open_ms_utc === 'number' && Number.isFinite(e.open_ms_utc)
          ? new Date(e.open_ms_utc).toISOString()
          : null;
        const close = typeof e.close_ms_utc === 'number' && Number.isFinite(e.close_ms_utc)
          ? new Date(e.close_ms_utc).toISOString()
          : null;
        if (open || close) return `${open ?? '—'} → ${close ?? '—'}`;
      }
      return String(entry);
    });
  }

  /** Options-companion checkbox rows, in stable display order. */
  readonly optionsFlags = [
    { key: 'optionsIncludeCalls' as const, label: 'calls', value: this.optionsIncludeCalls },
    { key: 'optionsIncludePuts' as const, label: 'puts', value: this.optionsIncludePuts },
    { key: 'optIncludeOhlcv' as const, label: 'OHLCV bars', value: this.optIncludeOhlcv },
    { key: 'optIncludeVwap' as const, label: 'VWAP', value: this.optIncludeVwap },
    { key: 'optIncludeTransactions' as const, label: 'transactions', value: this.optIncludeTransactions },
    { key: 'optIncludeOi' as const, label: 'open interest', value: this.optIncludeOi },
    { key: 'optIncludeIv' as const, label: 'implied volatility', value: this.optIncludeIv },
    { key: 'optIncludeDelta' as const, label: 'delta', value: this.optIncludeDelta },
    { key: 'optIncludeGamma' as const, label: 'gamma', value: this.optIncludeGamma },
    { key: 'optIncludeTheta' as const, label: 'theta', value: this.optIncludeTheta },
    { key: 'optIncludeVega' as const, label: 'vega', value: this.optIncludeVega },
    { key: 'optIncludeRho' as const, label: 'rho', value: this.optIncludeRho },
    { key: 'optIncludeDiscontinuity' as const, label: 'discontinuity flags', value: this.optIncludeDiscontinuity },
  ];

  /** Reference/quality companion checkbox rows. */
  readonly companionFlags: readonly {
    key: DataCompanionKey;
    label: string;
  }[] = [
    { key: 'includePreviousClose', label: 'previous_close.csv' },
    { key: 'includeSplits', label: 'splits.csv' },
    { key: 'includeDividends', label: 'dividends.csv' },
    { key: 'includeTickerOverview', label: 'ticker_overview.csv' },
    { key: 'includeNews', label: 'news.csv' },
    { key: 'includeFinancials', label: 'financials.csv' },
    { key: 'includeStockTrades', label: 'stock_trades.csv' },
    { key: 'includeStockQuotes', label: 'stock_quotes.csv' },
  ];

  /** Latest committed window date for the past-chain inspector. */
  readonly analysisDate = computed(() => {
    const window = this.store.committedWindow();
    if (!window) return '';
    return new Date(window.endMsUtc).toISOString().slice(0, 10);
  });
}

type OptionsFlagKey =
  | 'optionsIncludeCalls'
  | 'optionsIncludePuts'
  | 'optIncludeOhlcv'
  | 'optIncludeVwap'
  | 'optIncludeTransactions'
  | 'optIncludeOi'
  | 'optIncludeIv'
  | 'optIncludeDelta'
  | 'optIncludeGamma'
  | 'optIncludeTheta'
  | 'optIncludeVega'
  | 'optIncludeRho'
  | 'optIncludeDiscontinuity';

type DataCompanionKey = Exclude<
  keyof DataLabCompanionSettings,
  'optionsCompanionEnabled' | 'includeQualityReport'
>;
