import {
  Component, inject, signal, computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe, UpperCasePipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { InputText } from 'primeng/inputtext';
import { Button } from 'primeng/button';
import { Select } from 'primeng/select';
import { SelectButton } from 'primeng/selectbutton';
import { ProgressSpinner } from 'primeng/progressspinner';
import { InputNumber } from 'primeng/inputnumber';
import { ToggleSwitch } from 'primeng/toggleswitch';
import { MarketDataService } from '../../services/market-data.service';
import { QuantLibService } from '../../services/quantlib.service';
import {
  SnapshotUnderlyingResult, SnapshotContractResult,
  StrategyAnalyzeResult, StrategyAnalyzeOptions, StrategyLegInput, PayoffPoint,
  GreekType, WhatIfScenario, ChartCurveData, GreekCurvePoint,
  PricingEngineType, QuantLibPriceResult, QuantLibEngine,
} from '../../graphql/types';
// NOTE (Phase 1.2 of docs/architecture/numerical-authority-migration-plan.md):
// All BS math has moved server-side to `app/services/strategy_engine.py` and
// `app/services/bs_greeks.py`. This component no longer imports from
// `../../utils/black-scholes`; that module is deprecated (Phase 1.3) and
// kept only for any non-strategy-lab consumers during the transition.
// Pre-analyze "live" curves use intrinsic-at-expiration only (no BS) — see
// `livePayoffCurve` below. Anything that requires BS math (current value,
// Greeks, what-if, POP, per-leg diagnostics) is sourced from `analysisResult`.
import { ExpirationRibbonComponent } from '../options-chain-v2/expiration-ribbon/expiration-ribbon.component';
import { PayoffChartComponent } from './payoff-chart/payoff-chart.component';
import { Checkbox } from 'primeng/checkbox';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

interface LegConfig {
  strike: number;
  optionType: 'call' | 'put';
  position: 'long' | 'short';
  premium: number;
  iv: number;
  quantity: number;
  enabled: boolean;
}

interface StrategyTemplate {
  name: string;
  legs: { optionType: 'call' | 'put'; position: 'long' | 'short'; strikeOffset: number }[];
}

@Component({
  selector: 'app-options-strategy-lab',
  standalone: true,
  imports: [
    FormsModule, DecimalPipe, UpperCasePipe,
    InputText, Button, Select, SelectButton, ProgressSpinner,
    InputNumber, ToggleSwitch, Checkbox,
    ExpirationRibbonComponent, PayoffChartComponent,
    PageHeaderComponent,
  ],
  templateUrl: './options-strategy-lab.component.html',
  styleUrls: ['./options-strategy-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsStrategyLabComponent {
  private marketDataService = inject(MarketDataService);
  private quantlibService = inject(QuantLibService);

  // Pricing engine toggle: 'legacy' (client-side A&S BS) vs 'quantlib' (server-side C++ QuantLib)
  pricingEngine = signal<PricingEngineType>('legacy');
  quantlibEngine = signal<QuantLibEngine>('analytic_bs');
  quantlibAvailable = signal<boolean | null>(null);
  quantlibLoading = signal(false);
  quantlibGreeks = signal<QuantLibPriceResult | null>(null);

  readonly pricingEngineOptions = [
    { label: 'Legacy (A&S BS)', value: 'legacy' as PricingEngineType },
    { label: 'QuantLib (C++)', value: 'quantlib' as PricingEngineType },
  ];

  readonly quantlibEngineOptions = [
    { label: 'Analytic BS', value: 'analytic_bs' as QuantLibEngine },
    { label: 'Binomial CRR', value: 'binomial_crr' as QuantLibEngine },
    { label: 'Binomial JR', value: 'binomial_jr' as QuantLibEngine },
    { label: 'Binomial LR', value: 'binomial_lr' as QuantLibEngine },
    { label: 'Finite Diff', value: 'finite_diff' as QuantLibEngine },
    { label: 'Monte Carlo', value: 'monte_carlo' as QuantLibEngine },
  ];

  // Step 1: Ticker + expirations + chain filters
  ticker = signal('SPY');
  availableExpirations = signal<string[]>([]);
  selectedExpiration = signal<string | null>(null);
  expirationsLoading = signal(false);

  // Polygon contract filters (prefilled with sensible defaults)
  filterContractType = signal<string | null>(null);
  filterExpirationRange = signal('6m');

  // Step 2: Chain data
  underlying = signal<SnapshotUnderlyingResult | null>(null);
  allContracts = signal<SnapshotContractResult[]>([]);
  chainLoading = signal(false);
  spotPriceOverride = signal<number | null>(null);

  // Step 3: Strategy config
  strategyType = signal('bull_call_spread');
  manualMode = signal(false);
  legs = signal<LegConfig[]>([
    { strike: 0, optionType: 'call', position: 'long', premium: 0, iv: 0, quantity: 1, enabled: true },
    { strike: 0, optionType: 'call', position: 'short', premium: 0, iv: 0, quantity: 1, enabled: true },
  ]);

  // Step 4: Results
  analysisResult = signal<StrategyAnalyzeResult | null>(null);
  analyzing = signal(false);
  error = signal<string | null>(null);

  // Chart enhancement: What-If + Greeks
  riskFreeRate = signal(0.043);
  priceRangePct = signal(0.20);
  selectedGreek = signal<GreekType>('delta');
  whatIfScenarios = signal<WhatIfScenario[]>([
    { id: 'time_plus5', label: 'T+5d', enabled: false, timeDeltaDays: 5, ivShift: 0, color: '#f59e0b' },
    { id: 'iv_up10', label: 'IV+10%', enabled: false, timeDeltaDays: 0, ivShift: 0.10, color: '#8b5cf6' },
    { id: 'iv_down10', label: 'IV−10%', enabled: false, timeDeltaDays: 0, ivShift: -0.10, color: '#22c55e' },
  ]);

  // Strategy templates
  readonly strategyOptions = [
    { label: 'Bull Call Spread', value: 'bull_call_spread' },
    { label: 'Bear Put Spread', value: 'bear_put_spread' },
    { label: 'Long Straddle', value: 'long_straddle' },
    { label: 'Iron Condor', value: 'iron_condor' },
    { label: 'Iron Butterfly', value: 'iron_butterfly' },
    { label: 'Covered Call', value: 'covered_call' },
    { label: 'Protective Put', value: 'protective_put' },
    { label: 'Custom', value: 'custom' },
  ];

  readonly positionOptions = [
    { label: 'Long', value: 'long' },
    { label: 'Short', value: 'short' },
  ];

  readonly typeOptions = [
    { label: 'Call', value: 'call' },
    { label: 'Put', value: 'put' },
  ];

  readonly greekOptions = [
    { label: 'Delta (Δ)', value: 'delta' },
    { label: 'Gamma (Γ)', value: 'gamma' },
    { label: 'Theta (Θ)', value: 'theta' },
    { label: 'Vega (V)', value: 'vega' },
    { label: 'Rho (ρ)', value: 'rho' },
  ];

  readonly rangeOptions = [
    { label: '±5%', value: 0.05 },
    { label: '±10%', value: 0.10 },
    { label: '±20%', value: 0.20 },
    { label: '±50%', value: 0.50 },
  ];

  readonly contractTypeOptions = [
    { label: 'All', value: null },
    { label: 'Calls', value: 'call' },
    { label: 'Puts', value: 'put' },
  ];

  readonly expirationRangeOptions = [
    { label: '1 Month', value: '1m' },
    { label: '3 Months', value: '3m' },
    { label: '6 Months', value: '6m' },
    { label: '1 Year', value: '1y' },
    { label: '2 Years', value: '2y' },
  ];

  private expirationDateLte(): string {
    const days: Record<string, number> = { '1m': 30, '3m': 90, '6m': 180, '1y': 365, '2y': 730 };
    const d = days[this.filterExpirationRange()] ?? 180;
    return new Date(Date.now() + d * 86400000).toISOString().slice(0, 10);
  }

  private readonly TEMPLATES: Record<string, StrategyTemplate> = {
    bull_call_spread: {
      name: 'Bull Call Spread',
      legs: [
        { optionType: 'call', position: 'long', strikeOffset: -1 },
        { optionType: 'call', position: 'short', strikeOffset: 1 },
      ],
    },
    bear_put_spread: {
      name: 'Bear Put Spread',
      legs: [
        { optionType: 'put', position: 'long', strikeOffset: 1 },
        { optionType: 'put', position: 'short', strikeOffset: -1 },
      ],
    },
    long_straddle: {
      name: 'Long Straddle',
      legs: [
        { optionType: 'call', position: 'long', strikeOffset: 0 },
        { optionType: 'put', position: 'long', strikeOffset: 0 },
      ],
    },
    iron_condor: {
      name: 'Iron Condor',
      legs: [
        { optionType: 'put', position: 'long', strikeOffset: -3 },
        { optionType: 'put', position: 'short', strikeOffset: -1 },
        { optionType: 'call', position: 'short', strikeOffset: 1 },
        { optionType: 'call', position: 'long', strikeOffset: 3 },
      ],
    },
    iron_butterfly: {
      name: 'Iron Butterfly',
      legs: [
        { optionType: 'put', position: 'long', strikeOffset: -2 },
        { optionType: 'put', position: 'short', strikeOffset: 0 },
        { optionType: 'call', position: 'short', strikeOffset: 0 },
        { optionType: 'call', position: 'long', strikeOffset: 2 },
      ],
    },
    covered_call: {
      name: 'Covered Call',
      legs: [
        { optionType: 'call', position: 'long', strikeOffset: 0 },
        { optionType: 'call', position: 'short', strikeOffset: 2 },
      ],
    },
    protective_put: {
      name: 'Protective Put',
      legs: [
        { optionType: 'call', position: 'long', strikeOffset: 0 },
        { optionType: 'put', position: 'long', strikeOffset: -2 },
      ],
    },
  };

  // Computed: sorted strikes from chain
  availableStrikes = computed(() => {
    const contracts = this.allContracts();
    const strikes = new Set<number>();
    for (const c of contracts) {
      if (c.strikePrice != null) strikes.add(c.strikePrice);
    }
    return [...strikes].sort((a, b) => a - b);
  });

  strikeOptions = computed(() =>
    this.availableStrikes().map(s => ({ label: `$${s.toFixed(2)}`, value: s }))
  );

  spotPrice = computed(() => this.spotPriceOverride() ?? this.underlying()?.price ?? 0);

  canAnalyze = computed(() => {
    const enabledLegs = this.legs().filter(l => l.enabled);
    return enabledLegs.length > 0
      && enabledLegs.every(leg => leg.strike > 0 && leg.premium >= 0 && leg.iv >= 0)
      && this.selectedExpiration() !== null
      && !this.analyzing();
  });

  // X-axis center: single-leg → strike, multi-leg → midpoint of min/max strikes.
  // Falls back to spot price if no enabled legs have strikes.
  chartCenter = computed(() => {
    const enabledStrikes = this.legs()
      .filter(l => l.enabled && l.strike > 0)
      .map(l => l.strike);
    if (enabledStrikes.length === 0) return this.spotPrice();
    if (enabledStrikes.length === 1) return enabledStrikes[0];
    const minK = Math.min(...enabledStrikes);
    const maxK = Math.max(...enabledStrikes);
    return (minK + maxK) / 2;
  });

  // Uniform price grid with exact strike injection.
  // Centered on the strategy's strike(s), not spot.
  priceGrid = computed<number[]>(() => {
    const center = this.chartCenter();
    if (center <= 0) return [];
    const pct = this.priceRangePct();
    const low = center * (1 - pct);
    const high = center * (1 + pct);

    const count = 1200;
    const gridSet = new Set<number>();
    for (let i = 0; i <= count; i++) {
      gridSet.add(Math.round((low + (high - low) * (i / count)) * 100) / 100);
    }

    // Inject exact strike prices so expiration payoff kinks land precisely
    for (const leg of this.legs()) {
      if (leg.enabled && leg.strike > low && leg.strike < high) {
        gridSet.add(leg.strike);
      }
    }

    return [...gridSet].sort((a, b) => a - b);
  });

  // Live payoff computation — updates instantly on any leg/spot change
  livePayoffCurve = computed<PayoffPoint[]>(() => {
    const currentLegs = this.legs().filter(l => l.enabled);
    const grid = this.priceGrid();
    if (grid.length === 0 || currentLegs.length === 0 || currentLegs.some(l => l.strike <= 0)) return [];

    return grid.map(price => {
      let totalPnl = 0;
      for (const leg of currentLegs) {
        const intrinsic = leg.optionType === 'call'
          ? Math.max(price - leg.strike, 0)
          : Math.max(leg.strike - price, 0);
        totalPnl += leg.position === 'long'
          ? (intrinsic - leg.premium) * leg.quantity
          : (leg.premium - intrinsic) * leg.quantity;
      }
      return { price, pnl: totalPnl };
    });
  });

  liveBreakevens = computed<number[]>(() => {
    const curve = this.livePayoffCurve();
    if (curve.length < 2) return [];

    const breakevens: number[] = [];
    for (let i = 0; i < curve.length - 1; i++) {
      const p1 = curve[i];
      const p2 = curve[i + 1];
      if ((p1.pnl <= 0 && p2.pnl > 0) || (p1.pnl >= 0 && p2.pnl < 0)) {
        const ratio = Math.abs(p1.pnl) / (Math.abs(p1.pnl) + Math.abs(p2.pnl));
        breakevens.push(Math.round((p1.price + (p2.price - p1.price) * ratio) * 100) / 100);
      }
    }
    return breakevens;
  });

  // Computed: fractional days/time to expiry (includes intraday precision)
  daysToExpiry = computed(() => {
    const exp = this.selectedExpiration();
    if (!exp) return 0;
    const expDate = new Date(exp + 'T16:00:00'); // market close
    const now = new Date();
    return Math.max((expDate.getTime() - now.getTime()) / 86400000, 0);
  });

  timeToExpiry = computed(() => this.daysToExpiry() / 365);

  // ---------------------------------------------------------------------------
  // Phase 1.2: BS-based curves come from the server.
  //
  // The component used to recompute Black-Scholes price + Greeks here on every
  // signal change. That's now the server's job (see
  // `app/services/strategy_engine.py::analyze_strategy` with the
  // `include_current_curve` / `include_greek_curves` / `include_leg_diagnostics`
  // flags). The signals below are simple selectors over `analysisResult` and a
  // companion `whatIfResults` map populated by parallel analyze calls.
  // ---------------------------------------------------------------------------

  // Per-what-if-scenario analyze results, keyed by scenario id.
  // Populated by `analyzeStrategy()` when scenarios are enabled.
  private whatIfResults = signal<Record<string, StrategyAnalyzeResult>>({});

  // Premium-weighted average IV across enabled legs. Pure arithmetic (no BS math).
  // The payoff chart uses this for a tooltip-side probability label; nothing
  // numerical that users compare downstream depends on it.
  weightedIv = computed(() => {
    const legs = this.legs().filter(l => l.enabled && l.iv > 0);
    if (legs.length === 0) return 0.2;
    const totalWeight = legs.reduce((s, l) => s + l.premium * l.quantity, 0);
    if (totalWeight <= 0) return legs.reduce((s, l) => s + l.iv, 0) / legs.length;
    return legs.reduce((s, l) => s + l.iv * l.premium * l.quantity, 0) / totalWeight;
  });

  // BS-priced P&L curve at current time. Sourced from the server payload.
  // Empty until the user clicks Analyze.
  currentPnlCurve = computed<PayoffPoint[]>(() => {
    const result = this.analysisResult();
    if (!result?.currentCurve) return [];
    return result.currentCurve.map(p => ({ price: p.price, pnl: p.theoreticalPnl }));
  });

  // What-if scenario curves. One server call per enabled scenario; results are
  // collected in `whatIfResults` keyed by scenario id.
  whatIfCurves = computed<ChartCurveData[]>(() => {
    const results = this.whatIfResults();
    return this.whatIfScenarios()
      .filter(s => s.enabled)
      .map(scenario => {
        const r = results[scenario.id];
        const points: PayoffPoint[] = r?.currentCurve
          ? r.currentCurve.map(p => ({ price: p.price, pnl: p.theoreticalPnl }))
          : [];
        return { label: scenario.label, points, color: scenario.color, borderDash: [6, 3] };
      });
  });

  // Greek curve for the right Y-axis. Server returns aggregate greeks per spot
  // grid point; this selector picks the requested greek.
  greekCurve = computed<GreekCurvePoint[]>(() => {
    const result = this.analysisResult();
    if (!result?.greekCurves) return [];
    const greek = this.selectedGreek();
    return result.greekCurves.map(p => ({
      price: p.price,
      value: greek === 'delta' ? p.delta
        : greek === 'gamma' ? p.gamma
        : greek === 'theta' ? p.theta
        : greek === 'vega'  ? p.vega
        : 0,
    }));
  });

  // ---------------------------------------------------------------------------
  // Probability of Profit — sourced from server (Phase 1.2 of migration plan).
  //
  // The server uses per-boundary IV interpolation to capture skew effects;
  // see `compute_pop` in `app/services/strategy_engine.py`. Returns null until
  // the user has run Analyze.
  // ---------------------------------------------------------------------------
  probabilityOfProfit = computed<number | null>(() => {
    const result = this.analysisResult();
    if (!result?.success) return null;
    return result.pop;
  });

  // ---------------------------------------------------------------------------
  // Diagnostic table: per-leg current theoretical + Greeks at the request spot.
  //
  // Sourced from the server's `legDiagnostics` payload (Phase 1.2 of migration
  // plan). The previous version computed a per-price × per-leg BS breakdown
  // including d1/d2/N(d1)/N(d2)/discount intermediate values; these were used
  // for debugging the client-side BS implementation. With BS now server-side,
  // those intermediates would have to round-trip through GraphQL on every
  // analyze call — they're not worth the bandwidth, so the simplified table
  // shows just what users actually compare: per-leg current value and per-leg
  // Greeks at request spot.
  // ---------------------------------------------------------------------------

  diagnosticRows = computed(() => {
    const result = this.analysisResult();
    if (!result?.legDiagnostics) return [];
    return result.legDiagnostics.map(row => ({
      legId: row.legId,
      strike: row.strike,
      optionType: row.optionType,
      position: row.position,
      premium: row.entryPremium,
      iv: row.iv,
      quantity: row.quantity,
      theoPrice: row.currentTheoretical,
      delta: row.currentDelta,
      gamma: row.currentGamma,
      theta: row.currentTheta,
      vega: row.currentVega,
      // Per-leg P&L sourced from the server (Phase 1.1 `legDiagnostics.legPnl`).
      // Sign is already baked in by `position` Python-side; UI just displays.
      legPnl: row.legPnl,
    }));
  });

  /** Summary metadata for the diagnostic panel header */
  diagnosticMeta = computed(() => {
    const spot = this.spotPrice();
    const t = this.timeToExpiry();
    const dte = this.daysToExpiry();
    const r = this.riskFreeRate();
    const enabledLegs = this.legs().filter(l => l.enabled && l.strike > 0);
    return { spot, t, dte, r, legs: enabledLegs };
  });

  // Fetch expirations
  async fetchExpirations(): Promise<void> {
    const tk = this.ticker().trim().toUpperCase();
    if (!tk) return;

    this.expirationsLoading.set(true);
    this.error.set(null);
    this.availableExpirations.set([]);
    this.selectedExpiration.set(null);
    this.allContracts.set([]);
    this.underlying.set(null);
    this.spotPriceOverride.set(null);
    this.analysisResult.set(null);

    try {
      const expirations = await firstValueFrom(
        this.marketDataService.getOptionsExpirations(tk, {
          contractType: this.filterContractType() ?? undefined,
          expirationDateLte: this.expirationDateLte(),
        })
      );
      this.availableExpirations.set(expirations);
    } catch (err: any) {
      this.error.set(err.message || 'Failed to fetch expirations');
    } finally {
      this.expirationsLoading.set(false);
    }
  }

  // Expiration selected → load chain
  async onExpirationSelected(date: string): Promise<void> {
    this.selectedExpiration.set(date);
    this.chainLoading.set(true);
    this.error.set(null);
    this.analysisResult.set(null);

    try {
      const result = await firstValueFrom(
        this.marketDataService.getOptionsChainSnapshot(this.ticker().trim().toUpperCase(), date)
      );

      if (!result.success) {
        this.error.set(result.error || 'Failed to load chain');
        return;
      }

      this.underlying.set(result.underlying);
      this.allContracts.set(result.contracts);
      // Auto-populate riskFreeRate from FRED-sourced rate (Step 8 of IV-RV alignment).
      // User can still override via UI.
      if (result.riskFreeRate != null && result.riskFreeRate > 0) {
        this.riskFreeRate.set(result.riskFreeRate);
      }
      this.applyTemplate();
    } catch (err: any) {
      this.error.set(err.message || 'Failed to load chain');
    } finally {
      this.chainLoading.set(false);
    }
  }

  // Strategy type changed
  onStrategyTypeChanged(): void {
    if (this.strategyType() !== 'custom') {
      this.applyTemplate();
    }
  }

  // Apply template and auto-populate legs from chain
  private applyTemplate(): void {
    const tmpl = this.TEMPLATES[this.strategyType()];
    if (!tmpl) return;

    const strikes = this.availableStrikes();
    const spot = this.spotPrice();
    if (strikes.length === 0 || spot === 0) return;

    // Find ATM index
    let atmIdx = 0;
    let minDist = Infinity;
    for (let i = 0; i < strikes.length; i++) {
      const d = Math.abs(strikes[i] - spot);
      if (d < minDist) { atmIdx = i; minDist = d; }
    }

    const contracts = this.allContracts();
    const newLegs: LegConfig[] = tmpl.legs.map(legTmpl => {
      const strikeIdx = Math.max(0, Math.min(strikes.length - 1, atmIdx + legTmpl.strikeOffset));
      const strike = strikes[strikeIdx];

      // Find matching contract
      const contract = contracts.find(
        c => c.strikePrice === strike && c.contractType === legTmpl.optionType
      );

      return {
        strike,
        optionType: legTmpl.optionType,
        position: legTmpl.position,
        premium: this.resolvePremium(contract),
        iv: contract?.impliedVolatility ?? 0,
        quantity: 1,
        enabled: true,
      };
    });

    this.legs.set(newLegs);
  }

  // Resolve premium: day.close → lastTrade.price → lastQuote.midpoint → (bid+ask)/2
  private resolvePremium(contract: SnapshotContractResult | null | undefined): number {
    if (!contract) return 0;
    if (contract.day?.close != null && contract.day.close > 0) return contract.day.close;
    if (contract.lastTrade?.price != null && contract.lastTrade.price > 0) return contract.lastTrade.price;
    if (contract.lastQuote?.midpoint != null && contract.lastQuote.midpoint > 0) return contract.lastQuote.midpoint;
    const bid = contract.lastQuote?.bid ?? 0;
    const ask = contract.lastQuote?.ask ?? 0;
    if (bid > 0 && ask > 0) return (bid + ask) / 2;
    return 0;
  }

  // Update a specific leg's strike and auto-fill premium/IV
  updateLegStrike(legIndex: number, newStrike: number): void {
    const currentLegs = [...this.legs()];
    const leg = { ...currentLegs[legIndex] };
    leg.strike = newStrike;

    if (!this.manualMode()) {
      const contract = this.allContracts().find(
        c => c.strikePrice === newStrike && c.contractType === leg.optionType
      );
      leg.premium = this.resolvePremium(contract);
      leg.iv = contract?.impliedVolatility ?? 0;
    }

    currentLegs[legIndex] = leg;
    this.legs.set(currentLegs);
  }

  updateLegField(legIndex: number, field: keyof LegConfig, value: any): void {
    const currentLegs = [...this.legs()];
    currentLegs[legIndex] = { ...currentLegs[legIndex], [field]: value };

    // If optionType changed and not manual, re-resolve premium/IV
    if (field === 'optionType' && !this.manualMode()) {
      const leg = currentLegs[legIndex];
      const contract = this.allContracts().find(
        c => c.strikePrice === leg.strike && c.contractType === leg.optionType
      );
      currentLegs[legIndex] = {
        ...leg,
        premium: this.resolvePremium(contract),
        iv: contract?.impliedVolatility ?? 0,
      };
    }

    this.legs.set(currentLegs);
  }

  addLeg(): void {
    const currentLegs = [...this.legs()];
    if (currentLegs.length >= 8) return;
    currentLegs.push({
      strike: this.availableStrikes()[0] ?? 0,
      optionType: 'call',
      position: 'long',
      premium: 0,
      iv: 0,
      quantity: 1,
      enabled: true,
    });
    this.legs.set(currentLegs);
    this.strategyType.set('custom');
  }

  removeLeg(index: number): void {
    const currentLegs = [...this.legs()];
    if (currentLegs.length <= 1) return;
    currentLegs.splice(index, 1);
    this.legs.set(currentLegs);
    this.strategyType.set('custom');
  }

  // What-if scenario management
  toggleWhatIf(id: string): void {
    this.whatIfScenarios.update(scenarios =>
      scenarios.map(s => s.id === id ? { ...s, enabled: !s.enabled } : s)
    );
  }

  addCustomWhatIf(): void {
    if (this.whatIfScenarios().length >= 8) return;
    const id = `custom_${Date.now()}`;
    this.whatIfScenarios.update(scenarios => [
      ...scenarios,
      { id, label: 'Custom', enabled: true, timeDeltaDays: 0, ivShift: 0, color: '#94a3b8' },
    ]);
  }

  removeWhatIf(id: string): void {
    this.whatIfScenarios.update(scenarios => scenarios.filter(s => s.id !== id));
  }

  // Analyze the strategy.
  //
  // Phase 1.2 of `docs/architecture/numerical-authority-migration-plan.md`:
  // Server is now the canonical source for current-time curves, Greek curves,
  // POP, and per-leg diagnostics. We set all opt-in flags so the response
  // includes everything the UI needs, then fire one parallel analyze call per
  // enabled what-if scenario so the chart can layer them on top.
  async analyzeStrategy(): Promise<void> {
    const spot = this.spotPrice();
    const expiration = this.selectedExpiration();
    if (!expiration || spot === 0) return;

    this.analyzing.set(true);
    this.error.set(null);
    this.whatIfResults.set({});

    try {
      const legInputs: StrategyLegInput[] = this.legs().filter(l => l.enabled).map((l, i) => ({
        legId: `leg_${i}`,
        strike: l.strike,
        optionType: l.optionType,
        position: l.position,
        premium: l.premium,
        iv: l.iv,
        quantity: l.quantity,
      }));

      const symbol = this.ticker().trim().toUpperCase();
      const baseOptions: StrategyAnalyzeOptions = {
        includeCurrentCurve: true,
        includeGreekCurves: true,
        includeLegDiagnostics: true,
      };

      // Primary analyze call (current state, no what-if shifts).
      const primary$ = this.marketDataService.analyzeOptionsStrategy(
        symbol, legInputs, expiration, spot, this.riskFreeRate(), baseOptions,
      );

      // One additional analyze call per enabled what-if scenario. Each carries
      // the same legs and includes the current curve under the shifted
      // (time, IV) assumption — that becomes the dashed comparison line.
      const enabledScenarios = this.whatIfScenarios().filter(s => s.enabled);
      const scenarioCalls = enabledScenarios.map(scenario =>
        firstValueFrom(this.marketDataService.analyzeOptionsStrategy(
          symbol, legInputs, expiration, spot, this.riskFreeRate(),
          {
            includeCurrentCurve: true,
            whatIfTimeShiftDays: scenario.timeDeltaDays,
            whatIfIvShift: scenario.ivShift,
          },
        )),
      );

      // Use allSettled so a single failed what-if scenario doesn't sink the
      // whole analysis. Primary is the core workflow; scenario overlays are
      // optional. Each scenario fails independently.
      const settled = await Promise.allSettled([
        firstValueFrom(primary$),
        ...scenarioCalls,
      ]);

      const primaryOutcome = settled[0];
      if (primaryOutcome.status === 'rejected') {
        this.error.set(primaryOutcome.reason?.message || 'Primary analysis failed');
        return;
      }
      const primary = primaryOutcome.value;
      if (!primary.success) {
        this.error.set(primary.error || 'Analysis failed');
        return;
      }

      this.analysisResult.set(primary);

      const scenarioResults: Record<string, StrategyAnalyzeResult> = {};
      enabledScenarios.forEach((scenario, idx) => {
        const outcome = settled[idx + 1];
        if (outcome.status === 'fulfilled' && outcome.value?.success) {
          scenarioResults[scenario.id] = outcome.value;
        } else if (outcome.status === 'rejected') {
          // Surface in console for diagnostics but don't block the primary render.
           
          console.warn(`[StrategyLab] What-if "${scenario.label}" failed:`, outcome.reason);
        }
      });
      this.whatIfResults.set(scenarioResults);

      // If QuantLib engine is selected, also fetch QuantLib Greeks for comparison
      if (this.pricingEngine() === 'quantlib') {
        await this.fetchQuantLibGreeks();
      }
    } catch (err: any) {
      this.error.set(err.message || 'Analysis failed');
    } finally {
      this.analyzing.set(false);
    }
  }

  // ----- QuantLib integration -----

  async checkQuantLibStatus(): Promise<void> {
    try {
      const status = await this.quantlibService.checkStatus();
      this.quantlibAvailable.set(status.available);
    } catch {
      this.quantlibAvailable.set(false);
    }
  }

  async fetchQuantLibGreeks(): Promise<void> {
    const spot = this.spotPrice();
    const expiration = this.selectedExpiration();
    const enabledLegs = this.legs().filter(l => l.enabled);
    if (!expiration || spot === 0 || enabledLegs.length === 0) return;

    this.quantlibLoading.set(true);
    try {
      const result = await this.quantlibService.priceStrategy({
        spot,
        legs: enabledLegs.map(l => ({
          strike: l.strike,
          optionType: l.optionType,
          position: l.position,
          premium: l.premium,
          iv: l.iv,
          quantity: l.quantity,
        })),
        expirationDate: expiration,
        riskFreeRate: this.riskFreeRate(),
        engine: this.quantlibEngine(),
      });

      if (result.success && result.legs.length > 0) {
        // Store the first leg's result for single-option diagnostics
        this.quantlibGreeks.set(result.legs[0] as any);
      }
    } catch (err: any) {
      console.error('[QuantLib] Error fetching Greeks:', err);
    } finally {
      this.quantlibLoading.set(false);
    }
  }

  onPricingEngineChange(engine: PricingEngineType): void {
    this.pricingEngine.set(engine);
    if (engine === 'quantlib' && this.quantlibAvailable() === null) {
      this.checkQuantLibStatus();
    }
  }

}