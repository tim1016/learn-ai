import {
  Component, inject, signal, computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { InputText } from 'primeng/inputtext';
import { Button } from 'primeng/button';
import { Select } from 'primeng/select';
import { SelectButton } from 'primeng/selectbutton';
import { ProgressSpinner } from 'primeng/progressspinner';
import { InputNumber } from 'primeng/inputnumber';
import { ToggleSwitch } from 'primeng/toggleswitch';
import { MarketDataService } from '../../services/market-data.service';
import {
  SnapshotUnderlyingResult, SnapshotContractResult,
  StrategyAnalyzeResult, StrategyLegInput, PayoffPoint,
  GreekType, WhatIfScenario, ChartCurveData, GreekCurvePoint,
} from '../../graphql/types';
import {
  strategyPnlAtPrice, strategyGreekAtPrice, LegParams, GreekName,
} from '../../utils/black-scholes';
import { ExpirationRibbonComponent } from '../options-chain-v2/expiration-ribbon/expiration-ribbon.component';
import { PayoffChartComponent } from './payoff-chart/payoff-chart.component';
import { Checkbox } from 'primeng/checkbox';

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
    FormsModule, DecimalPipe,
    InputText, Button, Select, SelectButton, ProgressSpinner,
    InputNumber, ToggleSwitch, Checkbox,
    ExpirationRibbonComponent, PayoffChartComponent,
  ],
  templateUrl: './options-strategy-lab.component.html',
  styleUrls: ['./options-strategy-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsStrategyLabComponent {
  private marketDataService = inject(MarketDataService);

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

  // Adaptive price grid — base uniform + dense zones around strikes for smooth curvature
  priceGrid = computed<number[]>(() => {
    const spot = this.spotPrice();
    if (spot <= 0) return [];
    const pct = this.priceRangePct();
    const low = spot * (1 - pct);
    const high = spot * (1 + pct);

    const gridSet = new Set<number>();

    // Base uniform grid (400 points)
    const baseCount = 400;
    for (let i = 0; i <= baseCount; i++) {
      gridSet.add(Math.round((low + (high - low) * (i / baseCount)) * 100) / 100);
    }

    // Dense zones: ±2% of spot around each unique enabled strike
    const enabledStrikes = new Set(
      this.legs().filter(l => l.enabled && l.strike > low && l.strike < high).map(l => l.strike),
    );
    const bandHalf = spot * 0.02;
    const denseCount = 200;
    for (const strike of enabledStrikes) {
      gridSet.add(strike);
      const bLow = Math.max(strike - bandHalf, low);
      const bHigh = Math.min(strike + bandHalf, high);
      for (let i = 0; i <= denseCount; i++) {
        gridSet.add(Math.round((bLow + (bHigh - bLow) * (i / denseCount)) * 100) / 100);
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

  // Enabled legs mapped to LegParams for BS utility
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

  // Premium-weighted average IV for tooltip probability
  weightedIv = computed(() => {
    const params = this.enabledLegsParams();
    if (params.length === 0) return 0.2;
    const valid = params.filter(l => l.iv > 0);
    if (valid.length === 0) return 0.2;
    const totalWeight = valid.reduce((s, l) => s + l.premium * l.quantity, 0);
    if (totalWeight <= 0) return valid.reduce((s, l) => s + l.iv, 0) / valid.length;
    return valid.reduce((s, l) => s + l.iv * l.premium * l.quantity, 0) / totalWeight;
  });

  // BS-priced P&L at current time (blue dashed line)
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

  // What-if scenario curves
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

  // Greek curve for right Y-axis
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

  // Analyze the strategy
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

}
