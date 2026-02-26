import {
  Component, inject, signal, computed,
  ChangeDetectionStrategy, OnInit, OnDestroy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { InputText } from 'primeng/inputtext';
import { Button } from 'primeng/button';
import { Tooltip } from 'primeng/tooltip';
import { Skeleton } from 'primeng/skeleton';
import { MarketDataService } from '../../services/market-data.service';
import {
  SnapshotUnderlyingResult, SnapshotContractResult,
  StrategyAnalyzeResult, StrategyLegInput, PayoffPoint,
  GreekType, WhatIfScenario, ChartCurveData, GreekCurvePoint,
  StockTickerSnapshot,
} from '../../graphql/types';
import {
  strategyPnlAtPrice, strategyGreekAtPrice, LegParams, GreekName,
} from '../../utils/black-scholes';
import { ExpirationRibbonComponent } from '../options-chain-v2/expiration-ribbon/expiration-ribbon.component';
import { PayoffChartComponent } from '../options-strategy-lab/payoff-chart/payoff-chart.component';

interface BuilderChainRow {
  strike: number;
  strikeFormatted: string;
  call: SnapshotContractResult | null;
  put: SnapshotContractResult | null;
  isAtm: boolean;
  itmCall: boolean;
  itmPut: boolean;
  otmCall: boolean;
  otmPut: boolean;
  callDelta: string;
  callPrice: string;
  callPriceNum: number;
  callIv: number;
  callOi: string;
  callVolume: string;
  callVolumeBarWidth: number;
  putDelta: string;
  putPrice: string;
  putPriceNum: number;
  putIv: number;
  putOi: string;
  putVolume: string;
  putVolumeBarWidth: number;
}

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
  selector: 'app-strategy-builder',
  standalone: true,
  imports: [
    FormsModule, DecimalPipe,
    InputText, Button, Tooltip, Skeleton,
    ExpirationRibbonComponent, PayoffChartComponent,
  ],
  templateUrl: './strategy-builder.component.html',
  styleUrls: ['./strategy-builder.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyBuilderComponent implements OnInit, OnDestroy {
  private marketDataService = inject(MarketDataService);

  // ── Input & Loading ───────────────────────────────────────
  ticker = signal('SPY');
  expirationsLoading = signal(false);
  chainLoading = signal(false);
  analyzing = signal(false);
  error = signal<string | null>(null);

  // ── Expiration State ──────────────────────────────────────
  availableExpirations = signal<string[]>([]);
  selectedExpiration = signal<string | null>(null);

  // ── Chain Data ────────────────────────────────────────────
  underlying = signal<SnapshotUnderlyingResult | null>(null);
  allContracts = signal<SnapshotContractResult[]>([]);
  stockSnapshot = signal<StockTickerSnapshot | null>(null);

  // ── Chain Display Controls ────────────────────────────────
  strikeRange = signal(15);
  readonly strikeRangeOptions = [5, 10, 15, 20, 25, 30];
  showAllStrikes = signal(false);

  // ── Strategy State ────────────────────────────────────────
  legs = signal<LegConfig[]>([]);
  strategyType = signal<string>('custom');
  drawerOpen = signal(false);

  // ── Analysis ──────────────────────────────────────────────
  analysisResult = signal<StrategyAnalyzeResult | null>(null);
  riskFreeRate = signal(0.043);
  priceRangePct = signal(0.05);
  selectedGreek = signal<GreekType>('delta');

  // ── What-If Scenarios ─────────────────────────────────────
  whatIfScenarios = signal<WhatIfScenario[]>([
    { id: 'time_plus5', label: 'T+5d', enabled: false, timeDeltaDays: 5, ivShift: 0, color: '#f59e0b' },
    { id: 'iv_up10', label: 'IV+10%', enabled: false, timeDeltaDays: 0, ivShift: 0.10, color: '#8b5cf6' },
    { id: 'iv_down10', label: 'IV−10%', enabled: false, timeDeltaDays: 0, ivShift: -0.10, color: '#22c55e' },
  ]);

  // ── Strategy Templates ────────────────────────────────────
  readonly strategyOptions = [
    { label: 'Custom', value: 'custom' },
    {
      label: 'Bullish',
      items: [
        { label: 'Bull Call Spread', value: 'bull_call_spread' },
        { label: 'Covered Call', value: 'covered_call' },
        { label: 'Naked Put', value: 'naked_put' },
        { label: 'Ratio Call Spread', value: 'ratio_call_spread' },
      ],
    },
    {
      label: 'Bearish',
      items: [
        { label: 'Bear Put Spread', value: 'bear_put_spread' },
        { label: 'Protective Put', value: 'protective_put' },
        { label: 'Naked Call', value: 'naked_call' },
        { label: 'Ratio Put Spread', value: 'ratio_put_spread' },
      ],
    },
    {
      label: 'Neutral / Volatility',
      items: [
        { label: 'Long Straddle', value: 'long_straddle' },
        { label: 'Short Straddle', value: 'short_straddle' },
        { label: 'Long Strangle', value: 'long_strangle' },
        { label: 'Short Strangle', value: 'short_strangle' },
        { label: 'Long Butterfly', value: 'long_butterfly' },
        { label: 'Short Butterfly', value: 'short_butterfly' },
        { label: 'Iron Condor', value: 'iron_condor' },
        { label: 'Iron Butterfly', value: 'iron_butterfly' },
      ],
    },
  ];

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
    short_straddle: {
      name: 'Short Straddle',
      legs: [
        { optionType: 'call', position: 'short', strikeOffset: 0 },
        { optionType: 'put', position: 'short', strikeOffset: 0 },
      ],
    },
    long_strangle: {
      name: 'Long Strangle',
      legs: [
        { optionType: 'call', position: 'long', strikeOffset: 2 },
        { optionType: 'put', position: 'long', strikeOffset: -2 },
      ],
    },
    short_strangle: {
      name: 'Short Strangle',
      legs: [
        { optionType: 'call', position: 'short', strikeOffset: 2 },
        { optionType: 'put', position: 'short', strikeOffset: -2 },
      ],
    },
    long_butterfly: {
      name: 'Long Butterfly',
      legs: [
        { optionType: 'call', position: 'long', strikeOffset: -2 },
        { optionType: 'call', position: 'short', strikeOffset: 0 },
        { optionType: 'call', position: 'short', strikeOffset: 0 },
        { optionType: 'call', position: 'long', strikeOffset: 2 },
      ],
    },
    short_butterfly: {
      name: 'Short Butterfly',
      legs: [
        { optionType: 'call', position: 'short', strikeOffset: -2 },
        { optionType: 'call', position: 'long', strikeOffset: 0 },
        { optionType: 'call', position: 'long', strikeOffset: 0 },
        { optionType: 'call', position: 'short', strikeOffset: 2 },
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
    naked_put: {
      name: 'Naked Put',
      legs: [
        { optionType: 'put', position: 'short', strikeOffset: -2 },
      ],
    },
    naked_call: {
      name: 'Naked Call',
      legs: [
        { optionType: 'call', position: 'short', strikeOffset: 2 },
      ],
    },
    ratio_call_spread: {
      name: 'Ratio Call Spread',
      legs: [
        { optionType: 'call', position: 'long', strikeOffset: 0 },
        { optionType: 'call', position: 'short', strikeOffset: 2 },
        { optionType: 'call', position: 'short', strikeOffset: 2 },
      ],
    },
    ratio_put_spread: {
      name: 'Ratio Put Spread',
      legs: [
        { optionType: 'put', position: 'long', strikeOffset: 0 },
        { optionType: 'put', position: 'short', strikeOffset: -2 },
        { optionType: 'put', position: 'short', strikeOffset: -2 },
      ],
    },
  };

  // ── Computed Signals ──────────────────────────────────────

  spotPrice = computed(() => {
    const snap = this.stockSnapshot();
    if (snap?.day?.close != null && snap.day.close > 0) return snap.day.close;
    return this.underlying()?.price ?? 0;
  });

  availableStrikes = computed(() => {
    const contracts = this.allContracts();
    const strikes = new Set<number>();
    for (const c of contracts) {
      if (c.strikePrice != null) strikes.add(c.strikePrice);
    }
    return [...strikes].sort((a, b) => a - b);
  });

  visibleRows = computed<BuilderChainRow[]>(() => {
    const contracts = this.allContracts();
    const price = this.spotPrice();
    const range = this.strikeRange();
    const showAll = this.showAllStrikes();

    if (contracts.length === 0) return [];

    const callMap = new Map<number, SnapshotContractResult>();
    const putMap = new Map<number, SnapshotContractResult>();
    const strikeSet = new Set<number>();

    for (const c of contracts) {
      if (c.strikePrice == null) continue;
      strikeSet.add(c.strikePrice);
      if (c.contractType === 'call') callMap.set(c.strikePrice, c);
      else if (c.contractType === 'put') putMap.set(c.strikePrice, c);
    }

    const strikes = [...strikeSet].sort((a, b) => a - b);

    let atmStrike: number | null = null;
    if (price > 0 && strikes.length > 0) {
      let minDist = Infinity;
      for (const s of strikes) {
        const dist = Math.abs(s - price);
        if (dist < minDist) { atmStrike = s; minDist = dist; }
      }
    }

    let visibleStrikes = strikes;
    if (!showAll && atmStrike != null) {
      const atmIdx = strikes.indexOf(atmStrike);
      if (atmIdx !== -1) {
        const start = Math.max(0, atmIdx - range);
        const end = Math.min(strikes.length, atmIdx + range + 1);
        visibleStrikes = strikes.slice(start, end);
      }
    }

    let maxCallVol = 0;
    let maxPutVol = 0;
    for (const s of visibleStrikes) {
      const cv = callMap.get(s)?.day?.volume ?? 0;
      const pv = putMap.get(s)?.day?.volume ?? 0;
      if (cv > maxCallVol) maxCallVol = cv;
      if (pv > maxPutVol) maxPutVol = pv;
    }

    return visibleStrikes.map(strike => {
      const call = callMap.get(strike) ?? null;
      const put = putMap.get(strike) ?? null;
      const isAtm = strike === atmStrike;

      return {
        strike,
        strikeFormatted: strike.toFixed(2),
        call,
        put,
        isAtm,
        itmCall: price > 0 && strike < price && strike !== atmStrike,
        itmPut: price > 0 && strike > price && strike !== atmStrike,
        otmCall: price > 0 && strike > price && strike !== atmStrike,
        otmPut: price > 0 && strike < price && strike !== atmStrike,
        callDelta: this.fmtGreek(call?.greeks?.delta ?? null),
        callPrice: this.resolvePrice(call),
        callPriceNum: this.resolvePremiumNum(call),
        callIv: call?.impliedVolatility ?? 0,
        callOi: this.fmtNum(call?.openInterest ?? null),
        callVolume: this.fmtNum(call?.day?.volume ?? null),
        callVolumeBarWidth: this.barWidth(call?.day?.volume ?? null, maxCallVol),
        putDelta: this.fmtGreek(put?.greeks?.delta ?? null),
        putPrice: this.resolvePrice(put),
        putPriceNum: this.resolvePremiumNum(put),
        putIv: put?.impliedVolatility ?? 0,
        putOi: this.fmtNum(put?.openInterest ?? null),
        putVolume: this.fmtNum(put?.day?.volume ?? null),
        putVolumeBarWidth: this.barWidth(put?.day?.volume ?? null, maxPutVol),
      };
    });
  });

  activeStrikeKeys = computed(() => {
    const keys = new Set<string>();
    for (const leg of this.legs()) {
      if (leg.enabled) {
        keys.add(`${leg.optionType}-${leg.strike}-${leg.position}`);
      }
    }
    return keys;
  });

  netCost = computed(() => {
    return this.legs().filter(l => l.enabled).reduce((sum, l) => {
      const sign = l.position === 'long' ? -1 : 1;
      return sum + sign * l.premium * l.quantity * 100;
    }, 0);
  });

  daysToExpiry = computed(() => {
    const exp = this.selectedExpiration();
    if (!exp) return 0;
    const expDate = new Date(exp + 'T16:00:00');
    const now = new Date();
    return Math.max((expDate.getTime() - now.getTime()) / 86400000, 0);
  });

  timeToExpiry = computed(() => this.daysToExpiry() / 365);

  enabledLegsParams = computed<LegParams[]>(() =>
    this.legs()
      .filter(l => l.enabled && l.strike > 0)
      .map(l => ({
        strike: l.strike,
        optionType: l.optionType,
        position: l.position,
        premium: l.premium,
        iv: l.iv,
        quantity: l.quantity,
      }))
  );

  weightedIv = computed(() => {
    const params = this.enabledLegsParams();
    if (params.length === 0) return 0.2;
    const valid = params.filter(l => l.iv > 0);
    if (valid.length === 0) return 0.2;
    const totalWeight = valid.reduce((s, l) => s + l.premium * l.quantity, 0);
    if (totalWeight <= 0) return valid.reduce((s, l) => s + l.iv, 0) / valid.length;
    return valid.reduce((s, l) => s + l.iv * l.premium * l.quantity, 0) / totalWeight;
  });

  // X-axis center: single-leg → strike, multi-leg → midpoint of min/max strikes.
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

  // Uniform price grid centered on strike(s) with exact strike injection.
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

  currentPnlCurve = computed<PayoffPoint[]>(() => {
    const params = this.enabledLegsParams();
    const t = this.timeToExpiry();
    const grid = this.priceGrid();
    if (grid.length === 0 || params.length === 0 || t <= 0) return [];

    const r = this.riskFreeRate();
    return grid.map(price => ({
      price,
      pnl: strategyPnlAtPrice(params, price, t, r),
    }));
  });

  whatIfCurves = computed<ChartCurveData[]>(() => {
    const params = this.enabledLegsParams();
    const dte = this.daysToExpiry();
    const grid = this.priceGrid();
    if (grid.length === 0 || params.length === 0) return [];

    const r = this.riskFreeRate();
    return this.whatIfScenarios()
      .filter(s => s.enabled)
      .map(scenario => {
        const newDte = Math.max(dte - scenario.timeDeltaDays, 0);
        const newT = newDte / 365;
        const shiftedParams = scenario.ivShift !== 0
          ? params.map(l => ({ ...l, iv: Math.max(l.iv + scenario.ivShift, 0.01) }))
          : params;

        const points: PayoffPoint[] = grid.map(price => ({
          price,
          pnl: strategyPnlAtPrice(shiftedParams, price, newT, r),
        }));

        return { label: scenario.label, points, color: scenario.color, borderDash: [6, 3] };
      });
  });

  greekCurve = computed<GreekCurvePoint[]>(() => {
    const params = this.enabledLegsParams();
    const t = this.timeToExpiry();
    const grid = this.priceGrid();
    if (grid.length === 0 || params.length === 0 || t <= 0) return [];

    const r = this.riskFreeRate();
    const greek = this.selectedGreek() as GreekName;
    return grid.map(price => ({
      price,
      value: strategyGreekAtPrice(params, price, t, r, greek),
    }));
  });

  liveMaxProfit = computed(() => {
    const curve = this.livePayoffCurve();
    if (curve.length === 0) return null;
    return Math.max(...curve.map(p => p.pnl));
  });

  liveMaxLoss = computed(() => {
    const curve = this.livePayoffCurve();
    if (curve.length === 0) return null;
    return Math.min(...curve.map(p => p.pnl));
  });

  liveGreeks = computed(() => {
    const params = this.enabledLegsParams();
    const t = this.timeToExpiry();
    const spot = this.spotPrice();
    const r = this.riskFreeRate();
    if (params.length === 0 || t <= 0 || spot <= 0) return null;
    return {
      delta: strategyGreekAtPrice(params, spot, t, r, 'delta'),
      gamma: strategyGreekAtPrice(params, spot, t, r, 'gamma'),
      theta: strategyGreekAtPrice(params, spot, t, r, 'theta'),
      vega: strategyGreekAtPrice(params, spot, t, r, 'vega'),
    };
  });

  canAnalyze = computed(() => {
    const enabledLegs = this.legs().filter(l => l.enabled);
    return enabledLegs.length > 0
      && enabledLegs.every(leg => leg.strike > 0 && leg.premium >= 0 && leg.iv >= 0)
      && this.selectedExpiration() !== null
      && !this.analyzing();
  });

  skeletonRows = Array.from({ length: 8 }, (_, i) => i);

  // ── Lifecycle ─────────────────────────────────────────────

  ngOnInit(): void {
    document.documentElement.classList.add('app-dark');
  }

  ngOnDestroy(): void {
    document.documentElement.classList.remove('app-dark');
  }

  // ── Data Fetching ─────────────────────────────────────────

  async fetchExpirations(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    this.expirationsLoading.set(true);
    this.error.set(null);
    this.availableExpirations.set([]);
    this.selectedExpiration.set(null);
    this.underlying.set(null);
    this.allContracts.set([]);
    this.stockSnapshot.set(null);
    this.legs.set([]);
    this.analysisResult.set(null);

    try {
      const [expirations, snapshotResult] = await Promise.all([
        firstValueFrom(this.marketDataService.getOptionsExpirations(t)),
        firstValueFrom(this.marketDataService.getStockSnapshot(t))
          .catch(() => null),
      ]);

      if (snapshotResult?.success && snapshotResult.snapshot) {
        this.stockSnapshot.set(snapshotResult.snapshot);
      }

      this.availableExpirations.set(expirations);

      if (expirations.length > 0) {
        const today = new Date().toISOString().slice(0, 10);
        const nearest = expirations.find(e => e >= today) ?? expirations[0];
        this.selectedExpiration.set(nearest);
        await this.fetchChainSnapshot(t, nearest);
      }
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.expirationsLoading.set(false);
    }
  }

  async onExpirationSelected(date: string): Promise<void> {
    this.selectedExpiration.set(date);
    this.legs.set([]);
    this.analysisResult.set(null);
    const t = this.ticker().trim().toUpperCase();
    if (t) {
      await this.fetchChainSnapshot(t, date);
    }
  }

  async fetchChainSnapshot(ticker: string, expiration: string): Promise<void> {
    this.chainLoading.set(true);
    this.error.set(null);

    try {
      const result = await firstValueFrom(
        this.marketDataService.getOptionsChainSnapshot(ticker, expiration)
      );

      if (!result.success) {
        this.error.set(result.error ?? 'Failed to fetch snapshot');
        return;
      }

      this.underlying.set(result.underlying);
      this.allContracts.set(result.contracts);

      setTimeout(() => this.scrollToAtm(), 100);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.chainLoading.set(false);
    }
  }

  // ── Leg Management ────────────────────────────────────────

  addLegFromChain(
    contract: SnapshotContractResult | null,
    side: 'call' | 'put',
    position: 'long' | 'short'
  ): void {
    if (!contract?.strikePrice) return;
    if (this.legs().length >= 8) return;

    const premium = this.resolvePremiumNum(contract);
    const iv = contract.impliedVolatility ?? 0;

    const existing = this.legs().findIndex(l =>
      l.strike === contract.strikePrice &&
      l.optionType === side &&
      l.position === position
    );

    if (existing >= 0) {
      this.legs.update(legs => legs.map((l, i) =>
        i === existing ? { ...l, quantity: l.quantity + 1 } : l
      ));
    } else {
      this.legs.update(legs => [...legs, {
        strike: contract.strikePrice!,
        optionType: side,
        position,
        premium,
        iv,
        quantity: 1,
        enabled: true,
      }]);
    }

    this.strategyType.set('custom');
    this.analysisResult.set(null);
    this.drawerOpen.set(true);
  }

  removeLeg(index: number): void {
    this.legs.update(legs => legs.filter((_, i) => i !== index));
    this.analysisResult.set(null);
  }

  clearLegs(): void {
    this.legs.set([]);
    this.strategyType.set('custom');
    this.analysisResult.set(null);
  }

  // ── Template Application ──────────────────────────────────

  onStrategyTypeChanged(): void {
    if (this.strategyType() !== 'custom') {
      this.applyTemplate();
    }
  }

  private applyTemplate(): void {
    const tmpl = this.TEMPLATES[this.strategyType()];
    if (!tmpl) return;

    const strikes = this.availableStrikes();
    const spot = this.spotPrice();
    if (strikes.length === 0 || spot === 0) return;

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

      const contract = contracts.find(
        c => c.strikePrice === strike && c.contractType === legTmpl.optionType
      );

      return {
        strike,
        optionType: legTmpl.optionType,
        position: legTmpl.position,
        premium: this.resolvePremiumNum(contract),
        iv: contract?.impliedVolatility ?? 0,
        quantity: 1,
        enabled: true,
      };
    });

    this.legs.set(newLegs);
    this.analysisResult.set(null);
    this.drawerOpen.set(true);
  }

  // ── What-If ───────────────────────────────────────────────

  toggleWhatIf(id: string): void {
    this.whatIfScenarios.update(scenarios =>
      scenarios.map(s => s.id === id ? { ...s, enabled: !s.enabled } : s)
    );
  }

  // ── Analysis ──────────────────────────────────────────────

  async analyzeStrategy(): Promise<void> {
    const spot = this.spotPrice();
    const expiration = this.selectedExpiration();
    if (!expiration || spot === 0) return;

    this.analyzing.set(true);
    this.error.set(null);

    try {
      const legInputs: StrategyLegInput[] = this.legs().filter(l => l.enabled).map(l => ({
        strike: l.strike,
        optionType: l.optionType,
        position: l.position,
        premium: l.premium,
        iv: l.iv,
        quantity: l.quantity,
      }));

      const result = await firstValueFrom(
        this.marketDataService.analyzeOptionsStrategy(
          this.ticker().trim().toUpperCase(),
          legInputs,
          expiration,
          spot,
        )
      );

      if (!result.success) {
        this.error.set(result.error || 'Analysis failed');
        return;
      }

      this.analysisResult.set(result);
    } catch (err: any) {
      this.error.set(err.message || 'Analysis failed');
    } finally {
      this.analyzing.set(false);
    }
  }

  // ── Helpers ───────────────────────────────────────────────

  isLegActive(optionType: 'call' | 'put', strike: number, position: 'long' | 'short'): boolean {
    return this.activeStrikeKeys().has(`${optionType}-${strike}-${position}`);
  }

  resolvePrice(c: SnapshotContractResult | null): string {
    if (!c) return '\u2014';
    if (c.day?.close != null) return c.day.close.toFixed(2);
    if (c.lastTrade?.price != null) return c.lastTrade.price.toFixed(2);
    if (c.lastQuote?.midpoint != null) return c.lastQuote.midpoint.toFixed(2);
    if (c.lastQuote?.bid != null && c.lastQuote?.ask != null) {
      return ((c.lastQuote.bid + c.lastQuote.ask) / 2).toFixed(2);
    }
    return '\u2014';
  }

  private resolvePremiumNum(contract: SnapshotContractResult | null | undefined): number {
    if (!contract) return 0;
    if (contract.day?.close != null && contract.day.close > 0) return contract.day.close;
    if (contract.lastTrade?.price != null && contract.lastTrade.price > 0) return contract.lastTrade.price;
    if (contract.lastQuote?.midpoint != null && contract.lastQuote.midpoint > 0) return contract.lastQuote.midpoint;
    const bid = contract.lastQuote?.bid ?? 0;
    const ask = contract.lastQuote?.ask ?? 0;
    if (bid > 0 && ask > 0) return (bid + ask) / 2;
    return 0;
  }

  private fmtGreek(val: number | null): string {
    return val != null ? val.toFixed(4) : '\u2014';
  }

  private fmtNum(val: number | null): string {
    return val != null ? val.toLocaleString() : '\u2014';
  }

  private barWidth(volume: number | null, max: number): number {
    if (!volume || !max) return 0;
    return (volume / max) * 100;
  }

  private scrollToAtm(): void {
    const el = document.querySelector('[data-atm="true"]');
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }
}
