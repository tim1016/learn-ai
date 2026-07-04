/* eslint-disable @typescript-eslint/no-explicit-any */
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
  createChart,
  CrosshairMode,
  LineSeries, LineStyle,
  type IChartApi, type ISeriesApi,
  type MouseEventParams,
  type UTCTimestamp,
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
import {
  bsDelta, bsGamma,
  bsPrice,
  bsRho,
  bsTheta, bsVega,
} from '../../utils/black-scholes';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

type ChartMetric = 'price' | GreekType;

interface ContractOption {
  label: string;
  value: SnapshotContractResult;
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
    description: 'Monte Carlo simulation with pseudorandom paths (seed=42, tolerance=0.001). Stochastic price paths sampled under risk-neutral measure. Greeks computed numerically.',
    library: 'C++ QuantLib MCEuropeanEngine(pseudorandom, tol=0.001)',
  },
];

@Component({
  selector: 'app-pricing-lab',
  standalone: true,
  imports: [FormsModule, DecimalPipe, TitleCasePipe, InputText, Button, Select, SelectButton, Skeleton, PageHeaderComponent],
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
  readonly riskFreeRate = signal(0.05);
  readonly spotRangePct = signal(20);

  // ── Loading states ───────────────────────────────────────────
  readonly expirationsLoading = signal(false);
  readonly chainLoading = signal(false);
  readonly compareLoading = signal(false);

  // ── Chain data ───────────────────────────────────────────────
  readonly availableExpirations = signal<string[]>([]);
  readonly selectedExpiration = signal<string | null>(null);
  readonly underlying = signal<{ ticker: string; price: number } | null>(null);
  readonly allContracts = signal<SnapshotContractResult[]>([]);

  // ── Selected contract ────────────────────────────────────────
  readonly selectedContract = signal<SnapshotContractResult | null>(null);

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
    if (iv && strike && optType) {
      const T = result.timeToExpiryYears;
      const r = this.riskFreeRate();
      if (T > 0) {
        const rangePct = this.spotRangePct() / 100;
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
      let maxDiff = 0, sumDiff = 0;
      for (let i = 0; i < len; i++) {
        const d = Math.abs(getVal(refCurve[i]) - getVal(curve[i]));
        maxDiff = Math.max(maxDiff, d);
        sumDiff += d;
      }
      stats.push({ key: def.key, label: def.label, maxDiff, avgDiff: len > 0 ? sumDiff / len : 0 });
    }
    return stats;
  });

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
    this.expirationsLoading.set(true);
    this.selectedContract.set(null);
    this.serverResult.set(null);

    try {
      const exps = await firstValueFrom(this.marketData.getOptionsExpirations(t));
      this.availableExpirations.set(exps);

      const today = new Date().toISOString().split('T')[0];
      const nearest = exps.find(e => e >= today) ?? exps[exps.length - 1];
      if (nearest) {
        this.selectedExpiration.set(nearest);
        await this.fetchChain(t, nearest);
      }
    } catch (err) {
      console.error('[PricingLab] Error fetching expirations:', err);
    } finally {
      this.expirationsLoading.set(false);
    }
  }

  async fetchChain(ticker: string, expiration: string): Promise<void> {
    this.chainLoading.set(true);
    this.selectedContract.set(null);
    this.serverResult.set(null);

    try {
      const snap = await firstValueFrom(
        this.marketData.getOptionsChainSnapshot(ticker, expiration),
      );
      if (snap.underlying) {
        this.underlying.set({
          ticker: snap.underlying.ticker ?? ticker,
          price: snap.underlying.price ?? 0,
        });
      }
      // Auto-populate the riskFreeRate signal from the FRED-sourced rate
      // returned by the snapshot. User can still override via UI.
      // (Step 8 of IV-RV alignment.)
      if (snap.riskFreeRate != null && snap.riskFreeRate > 0) {
        this.riskFreeRate.set(snap.riskFreeRate);
      }
      this.allContracts.set(snap.contracts ?? []);
    } catch (err) {
      console.error('[PricingLab] Error fetching chain:', err);
    } finally {
      this.chainLoading.set(false);
    }
  }

  async onExpirationChange(exp: string): Promise<void> {
    this.selectedExpiration.set(exp);
    await this.fetchChain(this.ticker(), exp);
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

    this.statusMessage.set({ type: 'info', text: 'Computing pricing curves across all models...' });
    this.compareLoading.set(true);
    try {
      const rangePct = this.spotRangePct() / 100;
      const result = await firstValueFrom(
        this.marketData.comparePricingModels({
          spot,
          strike,
          volatility: iv,
          expirationDate: exp,
          optionType: optType,
          riskFreeRate: this.riskFreeRate(),
          spotMin: spot * (1 - rangePct),
          spotMax: spot * (1 + rangePct),
          numPoints: 100,
        }),
      );

      if (!result.success) {
        this.statusMessage.set({
          type: 'error',
          text: `Server error: ${result.error || 'Unknown error'}. The computation may have timed out — try again.`,
        });
        this.serverResult.set(null);
        return;
      }

      const modelCount = result.models?.length ?? 0;
      this.serverResult.set(result);
      this.statusMessage.set({
        type: 'success',
        text: `Loaded ${modelCount} server model${modelCount !== 1 ? 's' : ''} + Legacy BS (client). Hover chart for values.`,
      });
    } catch (err: any) {
      console.error('[PricingLab] Comparison failed:', err);
      this.statusMessage.set({
        type: 'error',
        text: `Request failed: ${err?.message || 'Network error'}. Check that the backend services are running.`,
      });
      this.serverResult.set(null);
    } finally {
      this.compareLoading.set(false);
    }
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
        attributionLogo: false,
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

    this.chart = createChart(container, this.createChartOptions(
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

    this.diffChart = createChart(container, this.createChartOptions(
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

      if (pts?.length && isVisible) {
        series.setData(
          pts.map(p => ({ time: p.spot as UTCTimestamp, value: getVal(p) })),
        );
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
        allDiffs.push(getVal(refCurve[i]) - getVal(pts[i]));
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
      const data: { time: UTCTimestamp; value: number }[] = [];
      for (let i = 0; i < len; i++) {
        data.push({
          time: refCurve[i].spot as UTCTimestamp,
          value: (getVal(refCurve[i]) - getVal(pts[i])) * scale,
        });
      }
      series.setData(data);
      series.applyOptions({ visible: true });
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
      const raw = param.seriesData.get(series) as any;
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
