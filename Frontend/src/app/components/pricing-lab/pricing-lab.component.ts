import { AssetIdentityComponent } from '../../shared/asset-identity';
import { OptionContractComponent, type OptionContract } from '../../shared/option-contract/option-contract.component';
import { DecimalPipe, TitleCasePipe } from '@angular/common';
import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed, effect,
  ElementRef,
  inject, Injector,
  OnDestroy,
  signal,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  CrosshairMode,
  LineSeries, LineStyle,
  type IChartApi, type ISeriesApi,
  type LineData,
  type MouseEventParams,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts';
import { Button } from 'primeng/button';
import { InputText } from 'primeng/inputtext';
import { Select } from 'primeng/select';
import { SelectButton } from 'primeng/selectbutton';
import { Skeleton } from 'primeng/skeleton';
import { firstValueFrom } from 'rxjs';
import {
  GreekType,
  PricingCompareResult, PricingPoint,
  SnapshotContractResult,
} from '../../graphql/types';
import { MarketDataService } from '../../services/market-data.service';
import { etIsoDate } from '../../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../../shared/option-contract/option-expiry-date';
import { formatTimestampDisplay } from '../../shared/timestamp/timestamp-display';
import { errorMessage } from '../../shared/errors/error-message';
import { createAppChart } from '../../shared/charts/chart-utils';
import {
  bsDelta, bsGamma,
  bsPrice,
  bsRho,
  bsTheta, bsVega,
} from '../../utils/black-scholes';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';

type ChartMetric = 'price' | GreekType;

interface ContractOption {
  label: string;
  value: SnapshotContractResult;
}

/** One chart point. A value the engine does not provide has none, which the chart draws as a gap, never as zero. */
function linePoint(spot: number, value: number | null): LineData | WhitespaceData {
  const time = spot as UTCTimestamp;
  return value === null ? { time } : { time, value };
}

/** a - b, or null when either side is a Greek its engine does not provide. */
function valueDiff(a: number | null, b: number | null): number | null {
  return a === null || b === null ? null : a - b;
}

// ── Model registry ─────────────────────────────────────────────
// Each entry defines the visual styling and metadata for one model.
export interface ModelDef {
  key: string;        // matches backend model name (or 'legacy_bs' for client)
  label: string;      // display name
  shortLabel: string;  // 2-3 char for legend
  color: string;
  lineStyle: LineStyle;
  lineWidth: 1 | 2 | 3 | 4;
  source: 'client' | 'server';
  description: string;
  library: string;
}

export const MODEL_REGISTRY: ModelDef[] = [
  {
    key: 'legacy_bs', label: 'Legacy BS (A&S)', shortLabel: 'LBS',
    color: 'rgba(250, 204, 21, 0.95)', lineStyle: LineStyle.Solid, lineWidth: 3,
    source: 'client',
    description: 'Client-side Black-Scholes using the Abramowitz & Stegun (1964) rational approximation for the normal CDF. Error bound |ε| < 1.5×10⁻⁷.',
    library: 'TypeScript (black-scholes.ts) — zero dependencies',
  },
  {
    key: 'python_bs', label: 'Python BS (scipy)', shortLabel: 'PyBS',
    color: 'rgba(34, 211, 238, 0.95)', lineStyle: LineStyle.Dashed, lineWidth: 2,
    source: 'server',
    description: 'Server-side analytical Black-Scholes using scipy.stats.norm.cdf for the normal CDF. Full double-precision accuracy.',
    library: 'Python — scipy.stats.norm, math',
  },
  {
    key: 'quantlib_bs', label: 'QuantLib Analytic BS', shortLabel: 'QL-A',
    color: 'rgba(217, 70, 239, 0.95)', lineStyle: LineStyle.SparseDotted, lineWidth: 2,
    source: 'server',
    description: 'QuantLib AnalyticEuropeanEngine — closed-form BS via the compiled C++ QuantLib library. Uses GeneralizedBlackScholesProcess with flat vol/rate/div term structures.',
    library: 'C++ QuantLib (SWIG Python bindings)',
  },
  {
    key: 'quantlib_crr', label: 'QuantLib Binomial CRR', shortLabel: 'CRR',
    color: 'rgba(52, 211, 153, 0.95)', lineStyle: LineStyle.Solid, lineWidth: 2,
    source: 'server',
    description: 'Cox-Ross-Rubinstein (1979) binomial lattice with 801 time steps. Up factor u = e^(σ√Δt), down factor d = 1/u. Converges to BS as steps → ∞.',
    library: 'C++ QuantLib BinomialVanillaEngine("CRR", 801)',
  },
  {
    key: 'quantlib_jr', label: 'QuantLib Binomial JR', shortLabel: 'JR',
    color: 'rgba(251, 146, 60, 0.95)', lineStyle: LineStyle.Dashed, lineWidth: 2,
    source: 'server',
    description: 'Jarrow-Rudd (1983) equal-probability binomial tree with 801 steps. Drift-adjusted: u = e^((r−σ²/2)Δt + σ√Δt). Reduces oscillation vs CRR.',
    library: 'C++ QuantLib BinomialVanillaEngine("JR", 801)',
  },
  {
    key: 'quantlib_lr', label: 'QuantLib Binomial LR', shortLabel: 'LR',
    color: 'rgba(129, 140, 248, 0.95)', lineStyle: LineStyle.SparseDotted, lineWidth: 2,
    source: 'server',
    description: 'Leisen-Reimer (1996) binomial tree with 801 steps. Uses Peizer-Pratt inversion for probabilities — converges faster than CRR/JR with fewer oscillations.',
    library: 'C++ QuantLib BinomialVanillaEngine("LR", 801)',
  },
  {
    key: 'quantlib_fd', label: 'QuantLib Finite Diff', shortLabel: 'FD',
    color: 'rgba(244, 114, 182, 0.95)', lineStyle: LineStyle.Solid, lineWidth: 1,
    source: 'server',
    description: 'Finite differences method (Crank-Nicolson) on an 801×800 grid. Solves the BS PDE numerically. Suited for American options and exotic payoffs.',
    library: 'C++ QuantLib FdBlackScholesVanillaEngine(801, 800)',
  },
  {
    key: 'quantlib_mc', label: 'QuantLib Monte Carlo', shortLabel: 'MC',
    color: 'rgba(163, 163, 163, 0.95)', lineStyle: LineStyle.LargeDashed, lineWidth: 1,
    source: 'server',
    description: 'Monte Carlo simulation with pseudorandom paths (seed=42, tolerance=0.001). Stochastic price paths sampled under risk-neutral measure. Delta and gamma are computed numerically; the engine provides no theta, vega or rho.',
    library: 'C++ QuantLib MCEuropeanEngine(pseudorandom, tol=0.001)',
  },
];

@Component({
  selector: 'app-pricing-lab',
  standalone: true,
  imports: [AssetIdentityComponent, OptionContractComponent, FormsModule, DecimalPipe, TitleCasePipe, InputText, Button, Select, SelectButton, Skeleton, PageHeaderComponent, SymbolPickerComponent],
  templateUrl: './pricing-lab.component.html',
  styleUrls: ['./pricing-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PricingLabComponent implements OnDestroy {
  private readonly marketData = inject(MarketDataService);
  private readonly injector = inject(Injector);

  // Expose registry to template
  readonly models = MODEL_REGISTRY;

  // ── User inputs ──────────────────────────────────────────────
  readonly ticker = signal('SPY');

  /** A pick is one deliberate act — load its expirations immediately. */
  onTickerPicked(ticker: string): void {
    this.ticker.set(ticker);
    void this.fetchExpirations();
  }

  /** Empty sends no rate, so Python prices at its one default (#2764). A chain load fills Python's live rate. */
  readonly riskFreeRate = signal<number | null>(null);
  readonly spotRangePct = signal(20);

  // ── Loading states ───────────────────────────────────────────
  readonly expirationsLoading = signal(false);
  readonly chainLoading = signal(false);
  readonly compareLoading = signal(false);

  // ── Chain data ───────────────────────────────────────────────
  readonly availableExpirations = signal<number[]>([]);
  readonly selectedExpiration = signal<number | null>(null);

  /** Select options: each expiry anchor rendered as its New York date. */
  readonly expirationOptions = computed(() =>
    this.availableExpirations().map(anchor => ({
      label: formatTimestampDisplay(anchor, { mode: 'date-et', granularity: 'short-date' }),
      value: anchor,
    })),
  );
  readonly underlying = signal<{ ticker: string; price: number } | null>(null);
  readonly allContracts = signal<SnapshotContractResult[]>([]);
  /** Count expiration / chain fetches; only the latest may write the page. */
  private expirationsRequest = 0;
  private chainRequest = 0;

  // ── Selected contract ────────────────────────────────────────
  readonly selectedContract = signal<SnapshotContractResult | null>(null);

  readonly selectedContractIdentity = computed<OptionContract | null>(() => {
    const contract = this.selectedContract();
    if (!contract || contract.strikePrice === null || contract.expirationMs === null
      || (contract.contractType !== 'call' && contract.contractType !== 'put')) return null;
    return {
      occ_symbol: contract.ticker ?? '', underlying: this.ticker(), expiry_ms: contract.expirationMs,
      right: contract.contractType, strike: contract.strikePrice,
    };
  });

  // ── Chart metric toggle ──────────────────────────────────────
  readonly selectedMetric = signal<ChartMetric>('price');
  readonly metricOptions = [
    { label: 'Price', value: 'price' as ChartMetric },
    { label: 'Delta', value: 'delta' as ChartMetric },
    { label: 'Gamma', value: 'gamma' as ChartMetric },
    { label: 'Theta', value: 'theta' as ChartMetric },
    { label: 'Vega', value: 'vega' as ChartMetric },
    { label: 'Rho', value: 'rho' as ChartMetric },
  ];

  // ── Visible model toggles ───────────────────────────────────
  readonly visibleModels = signal<Set<string>>(
    new Set(MODEL_REGISTRY.map(m => m.key)),
  );

  // ── Docs panel toggle ────────────────────────────────────────
  readonly showDocs = signal(false);

  // ── Status message for user feedback ─────────────────────────
  readonly statusMessage = signal<{ type: 'info' | 'warn' | 'error' | 'success'; text: string } | null>(null);

  // ── Comparison result from server ────────────────────────────
  readonly serverResult = signal<PricingCompareResult | null>(null);

  // ── Diff scale factor ────────────────────────────────────────
  readonly diffScaleFactor = signal(1);

  // ── Diff reference model ─────────────────────────────────────
  readonly diffReference = signal<string>('legacy_bs');
  readonly diffReferenceOptions = MODEL_REGISTRY.map(m => ({ label: m.label, value: m.key }));

  // ── Contract picker options ──────────────────────────────────
  readonly callContracts = computed<ContractOption[]>(() => {
    return this.allContracts()
      .filter(c => c.contractType === 'call' && c.strikePrice != null)
      .sort((a, b) => (a.strikePrice ?? 0) - (b.strikePrice ?? 0))
      .map(c => ({
        label: `$${c.strikePrice} C ${this.formatIv(c.impliedVolatility)}`,
        value: c,
      }));
  });

  readonly putContracts = computed<ContractOption[]>(() => {
    return this.allContracts()
      .filter(c => c.contractType === 'put' && c.strikePrice != null)
      .sort((a, b) => (a.strikePrice ?? 0) - (b.strikePrice ?? 0))
      .map(c => ({
        label: `$${c.strikePrice} P ${this.formatIv(c.impliedVolatility)}`,
        value: c,
      }));
  });

  readonly contractTypeFilter = signal<'call' | 'put'>('call');
  readonly contractTypeOptions = [
    { label: 'Calls', value: 'call' as const },
    { label: 'Puts', value: 'put' as const },
  ];

  readonly filteredContracts = computed(() => {
    return this.contractTypeFilter() === 'call'
      ? this.callContracts()
      : this.putContracts();
  });

  // ── All model curves (legacy computed client-side, rest from server) ──
  private readonly allCurves = computed<Map<string, PricingPoint[]>>(() => {
    const map = new Map<string, PricingPoint[]>();
    const result = this.serverResult();
    const contract = this.selectedContract();
    const spot = this.underlying()?.price;
    if (!result || !contract || !spot) return map;

    // Legacy BS — client-side
    const iv = contract.impliedVolatility;
    const strike = contract.strikePrice;
    const optType = contract.contractType as 'call' | 'put';
    // Price the overlay at the rate the server priced at, never at a rate of its own (#2764).
    const r = result.riskFreeRate;
    if (iv && strike && optType && r != null) {
      const T = result.timeToExpiryYears;
      const rangePct = this.spotRangeFraction();
      if (T > 0 && rangePct !== null) {
        const spotMin = spot * (1 - rangePct);
        const spotMax = spot * (1 + rangePct);
        const numPoints = 100;
        const step = (spotMax - spotMin) / (numPoints - 1);
        const pts: PricingPoint[] = [];
        for (let i = 0; i < numPoints; i++) {
          const s = spotMin + i * step;
          pts.push({
            spot: Math.round(s * 10000) / 10000,
            price: bsPrice(s, strike, r, iv, T, optType),
            delta: bsDelta(s, strike, r, iv, T, optType),
            gamma: bsGamma(s, strike, r, iv, T),
            theta: bsTheta(s, strike, r, iv, T, optType),
            vega: bsVega(s, strike, r, iv, T),
            rho: bsRho(s, strike, r, iv, T, optType),
          });
        }
        map.set('legacy_bs', pts);
      }
    }

    // Server models
    for (const model of result.models ?? []) {
      map.set(model.model, model.points);
    }

    return map;
  });

  // ── Summary stats (pairwise max/avg diff vs reference) ───────
  readonly summaryStats = computed(() => {
    const curves = this.allCurves();
    const ref = this.diffReference();
    const refCurve = curves.get(ref);
    if (!refCurve?.length) return [];

    const metric = this.selectedMetric();
    const getVal = (p: PricingPoint) => metric === 'price' ? p.price : p[metric];

    const stats: { key: string; label: string; maxDiff: number; avgDiff: number }[] = [];
    for (const def of MODEL_REGISTRY) {
      if (def.key === ref) continue;
      const curve = curves.get(def.key);
      if (!curve?.length) continue;
      const len = Math.min(refCurve.length, curve.length);
      let maxDiff = 0, sumDiff = 0, compared = 0;
      for (let i = 0; i < len; i++) {
        const diff = valueDiff(getVal(refCurve[i]), getVal(curve[i]));
        if (diff === null) continue;
        const d = Math.abs(diff);
        maxDiff = Math.max(maxDiff, d);
        sumDiff += d;
        compared++;
      }
      if (compared === 0) continue;
      stats.push({ key: def.key, label: def.label, maxDiff, avgDiff: sumDiff / compared });
    }
    return stats;
  });

  /** The models with no value of the selected Greek: an engine does not provide every one (Monte Carlo has no theta). */
  readonly metricNotProvidedBy = computed(() => {
    const metric = this.selectedMetric();
    if (metric === 'price') return [];
    const curves = this.allCurves();
    return MODEL_REGISTRY
      .filter(def => {
        const pts = curves.get(def.key);
        return !!pts?.length && pts.every(p => p[metric] === null);
      })
      .map(def => def.shortLabel);
  });

  /** Range % as a fraction of spot; null when it is empty, 0 or past 100, which no curve spans. */
  private spotRangeFraction(): number | null {
    const pct = this.spotRangePct();
    return Number.isFinite(pct) && pct > 0 && pct < 100 ? pct / 100 : null;
  }

  // ── Chart refs ───────────────────────────────────────────────
  private chartEl = viewChild<ElementRef<HTMLDivElement>>('chartContainer');
  private tooltipEl = viewChild<ElementRef<HTMLDivElement>>('tooltip');
  private chart: IChartApi | null = null;
  private seriesMap = new Map<string, ISeriesApi<'Line'>>();

  // ── Diff chart refs ──────────────────────────────────────────
  private diffChartEl = viewChild<ElementRef<HTMLDivElement>>('diffChartContainer');
  private diffChart: IChartApi | null = null;
  private diffSeriesMap = new Map<string, ISeriesApi<'Line'>>();

  constructor() {
    afterNextRender(() => {
      this.bootstrapChart();
      this.bootstrapDiffChart();
      effect(() => this.syncChartData(), { injector: this.injector });
    });
  }

  // ── Data fetching ────────────────────────────────────────────

  async fetchExpirations(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;
    this.ticker.set(t);
    // A late reply for a ticker the user has since left must not write this one's selectors.
    const request = ++this.expirationsRequest;
    // Abandon the old ticker's chain fetch too: its loading state ends here.
    this.chainRequest++;
    this.chainLoading.set(false);
    this.expirationsLoading.set(true);
    this.statusMessage.set(null);
    this.availableExpirations.set([]);
    this.selectedExpiration.set(null);
    this.allContracts.set([]);
    this.underlying.set(null);
    this.selectedContract.set(null);
    this.serverResult.set(null);

    try {
      const exps = await firstValueFrom(this.marketData.getOptionsExpirations(t));
      if (request !== this.expirationsRequest) return;
      this.availableExpirations.set(exps);

      const todayAnchor = optionExpiryAnchorMs(etIsoDate(Date.now()));
      const nearest = exps.find(e => e >= todayAnchor) ?? exps[exps.length - 1];
      if (nearest) {
        this.selectedExpiration.set(nearest);
        await this.fetchChain(t, nearest);
      }
    } catch (err) {
      if (request === this.expirationsRequest) {
        this.statusMessage.set({ type: 'error', text: `Could not load expirations for ${t}: ${errorMessage(err)}` });
      }
    } finally {
      // Only the latest fetch ends the loading state it started.
      if (request === this.expirationsRequest) this.expirationsLoading.set(false);
    }
  }

  async fetchChain(ticker: string, expirationMs: number): Promise<void> {
    const request = ++this.chainRequest;
    this.chainLoading.set(true);
    this.statusMessage.set(null);
    this.selectedContract.set(null);
    this.serverResult.set(null);
    // The old expiry's strikes must not stay in the selectors if this fetch fails.
    this.allContracts.set([]);

    try {
      const snap = await firstValueFrom(
        this.marketData.getOptionsChainSnapshot(ticker, expirationMs),
      );
      if (request !== this.chainRequest) return;
      if (!snap.success) {
        this.statusMessage.set({
          type: 'error',
          text: `Could not load the ${ticker} chain: ${snap.error ?? 'no reason given'}`,
        });
        return;
      }
      if (snap.underlying) {
        this.underlying.set({
          ticker: snap.underlying.ticker ?? ticker,
          price: snap.underlying.price ?? 0,
        });
      }
      // Auto-populate the riskFreeRate signal from the FRED-sourced rate
      // returned by the snapshot. User can still override via UI.
      // (Step 8 of IV-RV alignment.)
      if (snap.riskFreeRate != null) {
        this.riskFreeRate.set(snap.riskFreeRate);
      }
      this.allContracts.set(snap.contracts ?? []);
    } catch (err) {
      if (request === this.chainRequest) {
        this.statusMessage.set({ type: 'error', text: `Could not load the ${ticker} chain: ${errorMessage(err)}` });
      }
    } finally {
      if (request === this.chainRequest) this.chainLoading.set(false);
    }
  }

  async onExpirationChange(expirationMs: number): Promise<void> {
    this.selectedExpiration.set(expirationMs);
    await this.fetchChain(this.ticker(), expirationMs);
  }

  async onContractSelect(contract: SnapshotContractResult): Promise<void> {
    this.selectedContract.set(contract);
    await this.runComparison();
  }

  async runComparison(): Promise<void> {
    const contract = this.selectedContract();
    const spot = this.underlying()?.price;
    if (!contract || !spot) {
      this.statusMessage.set({
        type: 'warn',
        text: !contract ? 'Please select a contract first.' : 'Underlying price not available. Try reloading the ticker.',
      });
      return;
    }

    const iv = contract.impliedVolatility;
    const strike = contract.strikePrice;
    const exp = this.selectedExpiration();
    const optType = contract.contractType;

    if (!iv || !strike || !exp || !optType) {
      const missing: string[] = [];
      if (!iv) missing.push('implied volatility');
      if (!strike) missing.push('strike price');
      if (!exp) missing.push('expiration');
      if (!optType) missing.push('option type');
      this.statusMessage.set({
        type: 'warn',
        text: `Cannot compare — missing: ${missing.join(', ')}. Try a different contract.`,
      });
      return;
    }

    const rangePct = this.spotRangeFraction();
    if (rangePct === null) {
      this.statusMessage.set({ type: 'warn', text: 'Range % must be above 0 and below 100.' });
      return;
    }

    this.statusMessage.set({ type: 'info', text: 'Computing pricing curves across all models...' });
    this.compareLoading.set(true);
    try {
      const result = await firstValueFrom(
        this.marketData.comparePricingModels({
          spot,
          strike,
          volatility: iv,
          expirationMs: exp,
          optionType: optType,
          riskFreeRate: this.riskFreeRate() ?? undefined,
          spotMin: spot * (1 - rangePct),
          spotMax: spot * (1 + rangePct),
          numPoints: 100,
        }),
      );

      if (!result.success) {
        this.statusMessage.set({
          type: 'error',
          text: `Could not compare: ${result.error || 'Unknown error'}.`,
        });
        this.serverResult.set(null);
        return;
      }

      this.serverResult.set(result);
      this.statusMessage.set(this.comparisonStatus(result));
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Network error';
      this.statusMessage.set({
        type: 'error',
        text: `Request failed: ${message}. Check that the backend services are running.`,
      });
      this.serverResult.set(null);
    } finally {
      this.compareLoading.set(false);
    }
  }

  /** How many of the engines asked for drew a curve, and why each of the others did not. */
  private comparisonStatus(result: PricingCompareResult): { type: 'success' | 'warn'; text: string } {
    const ran = result.models.length;
    const skipped = result.unavailableModels;
    const loaded = `Loaded ${ran} of ${ran + skipped.length} server engines + Legacy BS (client).`;
    if (skipped.length === 0) {
      return { type: 'success', text: `${loaded} Hover chart for values.` };
    }
    const modelsByReason = new Map<string, string[]>();
    for (const { model, reason } of skipped) {
      const label = MODEL_REGISTRY.find(def => def.key === model)?.shortLabel ?? model;
      modelsByReason.set(reason, [...(modelsByReason.get(reason) ?? []), label]);
    }
    const why = [...modelsByReason].map(([reason, labels]) => `${labels.join(', ')}: ${reason}`).join('; ');
    return { type: 'warn', text: `${loaded} Not run — ${why}.` };
  }

  // ── Model visibility toggle ──────────────────────────────────

  toggleModel(key: string): void {
    const s = new Set(this.visibleModels());
    if (s.has(key)) { s.delete(key); } else { s.add(key); }
    this.visibleModels.set(s);
  }

  isModelVisible(key: string): boolean {
    return this.visibleModels().has(key);
  }

  // ── Chart bootstrap ──────────────────────────────────────────

  private createChartOptions(priceFormatter: (p: number) => string) {
    return {
      autoSize: true,
      layout: {
        background: { color: 'transparent' },
        textColor: '#9ca3af',
        fontFamily: "'Inter', system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.06)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.06)' },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: {
        borderVisible: false,
        fixLeftEdge: true,
        fixRightEdge: true,
        tickMarkFormatter: (time: number) => `$${Number(time).toFixed(0)}`,
      },
      crosshair: { mode: CrosshairMode.Normal },
      localization: {
        priceFormatter,
        timeFormatter: (time: number) => `$${Number(time).toFixed(2)}`,
      },
    };
  }

  private bootstrapChart(): void {
    const container = this.chartEl()?.nativeElement;
    if (!container) return;

    this.chart = createAppChart(container, this.createChartOptions(
      (p: number) => p.toFixed(4),
    ));

    // Create one series per model
    for (const def of MODEL_REGISTRY) {
      const series = this.chart.addSeries(LineSeries, {
        color: def.color,
        lineWidth: def.lineWidth,
        lineStyle: def.lineStyle,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: def.lineWidth + 1,
        title: def.shortLabel,
        visible: true,
      });
      this.seriesMap.set(def.key, series);
    }

    this.chart.subscribeCrosshairMove(p => this.onCrosshair(p));
  }

  private bootstrapDiffChart(): void {
    const container = this.diffChartEl()?.nativeElement;
    if (!container) return;

    this.diffChart = createAppChart(container, this.createChartOptions(
      (p: number) => p.toExponential(2),
    ));

    // One diff series per non-reference model
    for (const def of MODEL_REGISTRY) {
      const series = this.diffChart.addSeries(LineSeries, {
        color: def.color,
        lineWidth: 2,
        lineStyle: def.lineStyle,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 3,
        title: def.shortLabel,
        visible: true,
      });
      this.diffSeriesMap.set(def.key, series);
    }
  }

  private ensureDiffChart(): void {
    if (this.diffChart) return;
    this.bootstrapDiffChart();
  }

  // ── Reactive chart sync ──────────────────────────────────────

  private syncChartData(): void {
    const curves = this.allCurves();
    const metric = this.selectedMetric();
    const visible = this.visibleModels();
    const diffRef = this.diffReference();

    if (!this.chart) return;

    const getVal = (p: PricingPoint) => metric === 'price' ? p.price : p[metric];

    // ── Main chart ──
    for (const def of MODEL_REGISTRY) {
      const series = this.seriesMap.get(def.key);
      if (!series) continue;

      const pts = curves.get(def.key);
      const isVisible = visible.has(def.key);

      if (pts?.length && isVisible && pts.some(p => getVal(p) !== null)) {
        series.setData(pts.map(p => linePoint(p.spot, getVal(p))));
        series.applyOptions({ visible: true });
      } else {
        series.setData([]);
        series.applyOptions({ visible: false });
      }
    }
    this.chart.timeScale().fitContent();

    // ── Diff chart ──
    this.ensureDiffChart();
    if (!this.diffChart) return;

    const refCurve = curves.get(diffRef);

    // Compute scale factor from all diffs
    const allDiffs: number[] = [];
    for (const def of MODEL_REGISTRY) {
      if (def.key === diffRef) continue;
      const pts = curves.get(def.key);
      if (!pts?.length || !refCurve?.length) continue;
      if (!visible.has(def.key)) continue;
      const len = Math.min(refCurve.length, pts.length);
      for (let i = 0; i < len; i++) {
        const diff = valueDiff(getVal(refCurve[i]), getVal(pts[i]));
        if (diff !== null) allDiffs.push(diff);
      }
    }

    const maxAbsDiff = allDiffs.length > 0 ? Math.max(...allDiffs.map(Math.abs)) : 0;
    let scale = 1;
    if (maxAbsDiff > 0 && maxAbsDiff < 0.001) {
      scale = Math.pow(10, Math.ceil(-Math.log10(maxAbsDiff)));
    }
    this.diffScaleFactor.set(scale);

    for (const def of MODEL_REGISTRY) {
      const series = this.diffSeriesMap.get(def.key);
      if (!series) continue;

      if (def.key === diffRef || !visible.has(def.key)) {
        series.setData([]);
        series.applyOptions({ visible: false });
        continue;
      }

      const pts = curves.get(def.key);
      if (!pts?.length || !refCurve?.length) {
        series.setData([]);
        series.applyOptions({ visible: false });
        continue;
      }

      const len = Math.min(refCurve.length, pts.length);
      const data: (LineData | WhitespaceData)[] = [];
      for (let i = 0; i < len; i++) {
        const diff = valueDiff(getVal(refCurve[i]), getVal(pts[i]));
        data.push(linePoint(refCurve[i].spot, diff === null ? null : diff * scale));
      }
      const plotted = data.some(d => 'value' in d);
      series.setData(plotted ? data : []);
      series.applyOptions({ visible: plotted });
    }
    this.diffChart.timeScale().fitContent();
  }

  // ── Crosshair tooltip ────────────────────────────────────────

  private onCrosshair(param: MouseEventParams): void {
    const tip = this.tooltipEl()?.nativeElement;
    if (!tip) return;

    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0) {
      tip.style.display = 'none';
      return;
    }

    const spot = Number(param.time);
    const metric = this.selectedMetric();
    const fmt = (v: number) => metric === 'price' ? `$${v.toFixed(4)}` : v.toFixed(6);

    let html = `<div class="tt-title">Spot: $${spot.toFixed(2)}</div>`;
    let anyValue = false;

    for (const def of MODEL_REGISTRY) {
      if (!this.visibleModels().has(def.key)) continue;
      const series = this.seriesMap.get(def.key);
      if (!series) continue;
      const raw = param.seriesData.get(series) as { value?: number } | undefined;
      const val = raw?.value;
      if (val == null) continue;
      anyValue = true;
      html += `<div class="tt-row"><span class="tt-dot" style="background:${def.color}"></span>${def.shortLabel}: ${fmt(val)}</div>`;
    }

    if (!anyValue) {
      tip.style.display = 'none';
      return;
    }

    tip.innerHTML = html;
    tip.style.display = 'block';

    const box = this.chartEl()?.nativeElement;
    if (!box) return;
    const tw = tip.offsetWidth;
    tip.style.left = (param.point.x + tw + 20 > box.clientWidth)
      ? `${param.point.x - tw - 12}px`
      : `${param.point.x + 12}px`;
    tip.style.top = `${Math.max(param.point.y - 20, 0)}px`;
  }

  // ── Helpers ──────────────────────────────────────────────────

  formatIv(iv: number | null): string {
    return iv != null ? `(IV ${(iv * 100).toFixed(1)}%)` : '';
  }

  // ── Cleanup ──────────────────────────────────────────────────

  ngOnDestroy(): void {
    this.chart?.remove();
    this.chart = null;
    this.diffChart?.remove();
    this.diffChart = null;
  }
}
