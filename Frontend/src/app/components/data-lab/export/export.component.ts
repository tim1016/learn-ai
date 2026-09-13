import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { RunSessionService } from '../../../services/run-session.service';
import { PastChainInspectorComponent } from '../past-chain-inspector/past-chain-inspector.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import type { DataLabCompanionSettings } from '../data-lab-workspace-store';
import {
  buildGenerateZipPayload,
  type OptionsCompanionWireConfig,
} from '../data-lab-request-mapper';
import { DataLabPlanService, type DataLabPlanReceipt } from './data-lab-plan.service';

/**
 * Data Lab Build dataset (PRD §7.4).
 *
 * NEVER mounts DataLabChartComponent and never calls the chart endpoint
 * (FR-004). The recipe is a progressive four-section form over the shared
 * workspace state; columns/sessions/estimates come from the Python plan
 * receipt rendered verbatim (FR-012); generation goes through
 * `RunSessionService.start()` — the ONLY submission path (FR-005).
 */
@Component({
  selector: 'app-data-lab-export',
  imports: [RouterLink, PastChainInspectorComponent, ReceiptLabelPipe],
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

  /** One mapper, one payload shape — plan and generate share it. */
  private buildPayload(): Record<string, unknown> | null {
    const ticker = this.store.committedTicker();
    const window = this.store.committedWindow();
    if (!ticker || !window) return null;
    const draft = this.store.draft();
    return buildGenerateZipPayload({
      ticker,
      window,
      indicators: this.store.indicators(),
      session: draft.session,
      forwardFill: draft.forwardFill,
      adjusted: draft.adjusted,
      companions: this.store.companions(),
      options: this.buildOptionsConfig(),
      warmup: this.warmup(),
      timespan: draft.timespan,
      multiplier: draft.multiplier,
      sort: this.sort(),
      limit: this.polygonLimit(),
    });
  }

  async loadPlan(): Promise<void> {
    const payload = this.buildPayload();
    if (!payload) {
      this.planError.set('Commit a ticker and window before previewing columns.');
      return;
    }
    this.planLoading.set(true);
    this.planError.set('');
    try {
      const receipt = await this.planService.plan(payload);
      this.store.setDatasetPlanReceipt(receipt as Record<string, unknown>);
    } catch (e: unknown) {
      this.planError.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.planLoading.set(false);
    }
  }

  /** The ONLY submission path (FR-005). */
  async generate(): Promise<void> {
    if (this.runActive()) return;
    const payload = this.buildPayload();
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
  /** Render a receipt block verbatim — no client-side reshaping. */
  pretty(value: unknown): string {
    return JSON.stringify(value, null, 2);
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
