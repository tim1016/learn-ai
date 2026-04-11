import {
  Component, signal, computed, inject,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { RouterModule } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { forkJoin } from 'rxjs';
import { DatePicker } from 'primeng/datepicker';
import { SharedModule } from 'primeng/api';
import { Tooltip } from 'primeng/tooltip';
import { AutoComplete, AutoCompleteCompleteEvent } from 'primeng/autocomplete';
import { environment } from '../../../environments/environment';
import { MarketDataService } from '../../services/market-data.service';
import { MarketMonitorService } from '../../services/market-monitor.service';
import { MarketHolidayEvent } from '../../models/market-monitor';
import {
  getDisabledHolidayDates,
  buildHolidayMap,
  getMinAllowedDate,
  validateDateRange,
} from '../../utils/date-validation';
import { LineChartComponent } from '../market-data/line-chart/line-chart.component';
import { ReplayControlsComponent } from './replay-controls/replay-controls.component';
import { ReplayChartComponent } from './replay-chart/replay-chart.component';
import { ReplayEngineService } from '../../services/replay-engine.service';
import { ReplayIndicatorService } from '../../services/replay-indicator.service';
import { ReplayStrategyService } from '../../services/replay-strategy.service';
import { StockAggregate } from '../../graphql/types';
import {
  StrategyLabChartComponent,
  ChartBar,
  ChartIndicatorResult,
} from './strategy-lab-chart/strategy-lab-chart.component';
import { BacktestResultsComponent } from './backtest-results/backtest-results.component';

export type LabMode = 'backtest' | 'replay';

// ── Python backtest response types ──
interface BacktestTradeResponse {
  trade_number: number;
  trade_type: string;
  entry_timestamp: string;
  exit_timestamp: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  cumulative_pnl_pct: number;
  signal_reason: string;
  indicator_snapshot: Record<string, number | null>;
}

// ── LEAN Statistics types (matching Python LeanStatisticsResponse) ──
export interface LeanPortfolioStats {
  average_win_rate: number;
  average_loss_rate: number;
  profit_loss_ratio: number;
  win_rate: number;
  loss_rate: number;
  expectancy: number;
  start_equity: number;
  end_equity: number;
  total_net_profit: number;
  compounding_annual_return: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  probabilistic_sharpe_ratio: number;
  annual_standard_deviation: number;
  annual_variance: number;
  alpha: number;
  beta: number;
  information_ratio: number;
  tracking_error: number;
  treynor_ratio: number;
  drawdown: number;
  drawdown_recovery: number;
  value_at_risk_99: number;
  value_at_risk_95: number;
  portfolio_turnover: number;
}

export interface LeanTradeStats {
  start_date_time: string;
  end_date_time: string;
  total_number_of_trades: number;
  number_of_winning_trades: number;
  number_of_losing_trades: number;
  total_profit_loss: number;
  total_profit: number;
  total_loss: number;
  largest_profit: number;
  largest_loss: number;
  average_profit_loss: number;
  average_profit: number;
  average_loss: number;
  average_trade_duration: string;
  average_winning_trade_duration: string;
  average_losing_trade_duration: string;
  max_consecutive_winning_trades: number;
  max_consecutive_losing_trades: number;
  profit_factor: number;
  profit_to_max_drawdown_ratio: number;
  profit_loss_standard_deviation: number;
  profit_loss_downside_deviation: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  total_fees: number;
}

export interface LeanRuntimeStats {
  equity: number;
  fees: number;
  net_profit: number;
  total_return: number;
  total_orders: number;
}

export interface LeanStatistics {
  portfolio: LeanPortfolioStats;
  trade: LeanTradeStats;
  runtime: LeanRuntimeStats;
}

interface BacktestResponse {
  success: boolean;
  ticker: string;
  strategy_name: string;
  parameters: Record<string, any>;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  win_loss_ratio: number;
  profit_factor: number;
  expectancy_per_trade: number;
  total_pnl_pct: number;
  total_pnl_pts: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  lean_statistics: LeanStatistics | null;
  source_bars: number;
  rth_bars: number;
  resampled_bars: number;
  bars_processed: number;
  timeframe: string;
  chart_bars: ChartBar[];
  chart_indicators: ChartIndicatorResult[];
  quality: any;
  trades: BacktestTradeResponse[];
  error: string | null;
}

export interface BacktestTradeForChart {
  trade_number: number;
  trade_type: string;
  entry_timestamp: string;
  exit_timestamp: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  cumulative_pnl_pct: number;
  signal_reason: string;
  indicator_snapshot: Record<string, number | null>;
}

@Component({
  selector: 'app-strategy-lab',
  standalone: true,
  imports: [
    CommonModule, FormsModule, RouterModule,
    DatePicker, SharedModule, Tooltip, AutoComplete,
    LineChartComponent,
    ReplayControlsComponent, ReplayChartComponent,
    BacktestResultsComponent,
  ],
  templateUrl: './strategy-lab.component.html',
  styleUrls: ['./strategy-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabComponent {
  private http = inject(HttpClient);
  private marketDataService = inject(MarketDataService);
  private marketMonitor = inject(MarketMonitorService);
  readonly replayEngine = inject(ReplayEngineService);
  readonly replayIndicators = inject(ReplayIndicatorService);
  readonly replayStrategy = inject(ReplayStrategyService);

  // ── Mode toggle ──
  mode = signal<LabMode>('backtest');

  // ── Replay state ──
  replayLoading = signal(false);
  replayDataLoaded = signal(false);
  replayOverlayLoading = signal(false);

  // ── Form inputs ──
  ticker = signal('AAPL');

  // Ticker autocomplete
  private readonly commonTickers = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'BRK.B',
    'JPM', 'V', 'UNH', 'MA', 'HD', 'PG', 'JNJ', 'COST', 'ABBV', 'MRK',
    'KO', 'PEP', 'AVGO', 'LLY', 'ADBE', 'CRM', 'NFLX', 'AMD', 'INTC',
    'SPY', 'QQQ', 'IWM', 'DIA', 'XLF', 'XLE', 'XLK', 'GLD', 'SLV',
    'BA', 'DIS', 'PYPL', 'SQ', 'SHOP', 'UBER', 'ABNB', 'COIN', 'SOFI',
  ];
  tickerSuggestions = signal<string[]>([]);

  // Date state: PrimeNG DatePicker binds to Date objects
  private static getYesterday(): Date {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    d.setHours(0, 0, 0, 0);
    return d;
  }
  private static get30DaysAgo(): Date {
    const d = StrategyLabComponent.getYesterday();
    d.setDate(d.getDate() - 30);
    return d;
  }
  private static formatDate(d: Date | null): string {
    if (!d) return '';
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  fromDateValue = signal<Date | null>(StrategyLabComponent.get30DaysAgo());
  toDateValue = signal<Date | null>(StrategyLabComponent.getYesterday());
  fromDate = computed(() => StrategyLabComponent.formatDate(this.fromDateValue()));
  toDate = computed(() => StrategyLabComponent.formatDate(this.toDateValue()));

  // Calendar constraints
  holidays = signal<MarketHolidayEvent[]>([]);
  disabledDates = computed(() => getDisabledHolidayDates(this.holidays()));
  holidayMap = computed(() => buildHolidayMap(this.holidays()));
  disabledDays: number[] = [0, 6];
  minDate = new Date(getMinAllowedDate() + 'T00:00:00');
  maxDate = StrategyLabComponent.getYesterday();

  // Date range validation
  dateRangeWarning = computed(() => validateDateRange(this.fromDate(), this.toDate()));

  // Pipeline options
  timespan = signal<'minute' | 'hour' | 'day'>('minute');
  multiplier = signal(5);
  session = signal<'rth' | 'extended'>('rth');
  forwardFill = signal(true);
  warmup = signal(true);

  // Strategy
  strategyName = signal<string>('sma_crossover');

  // Strategy param signals
  shortWindow = signal(10);
  longWindow = signal(30);
  rsiWindow = signal(14);
  oversold = signal(30);
  overbought = signal(70);
  emaCrossoverFastPeriod = signal(5);
  emaCrossoverSlowPeriod = signal(10);
  emaCrossoverRsiPeriod = signal(14);
  emaCrossoverAdxPeriod = signal(14);
  emaCrossoverMinGap = signal(0.20);
  emaCrossoverRsiMin = signal(50);
  emaCrossoverRsiMax = signal(70);
  emaCrossoverExitBars = signal(5);
  momentumRsiLength = signal(14);
  momentumRsiLow = signal(40);
  momentumRsiHigh = signal(60);
  momentumFastMa = signal(20);
  momentumSlowMa = signal(50);
  momentumStochK = signal(14);
  momentumStochD = signal(3);
  momentumExitMinutes = signal(15);
  rsiReversalWindow = signal(14);
  rsiReversalOversold = signal(30);
  rsiReversalOverbought = signal(70);

  // ── UI toggles ──
  showAdvanced = signal(false);
  showTradeLog = signal(false);
  showStrategyParams = signal(false);
  activePreset = signal<string | null>(null);

  // Strategy card: human-readable rules preview
  strategyMeta = computed(() => {
    const s = this.strategyName();
    const meta: Record<string, { display: string; rules: string[] }> = {
      sma_crossover: {
        display: 'SMA Crossover',
        rules: [
          `Entry: SMA ${this.shortWindow()} crosses above SMA ${this.longWindow()}`,
          `Exit: SMA ${this.shortWindow()} crosses below SMA ${this.longWindow()}`,
        ],
      },
      rsi_mean_reversion: {
        display: 'RSI Mean Reversion',
        rules: [
          `Buy when RSI(${this.rsiWindow()}) < ${this.oversold()}`,
          `Sell when RSI(${this.rsiWindow()}) > ${this.overbought()}`,
        ],
      },
      momentum_rsi_stochastic: {
        display: 'Momentum RSI + Stochastic',
        rules: [
          `RSI(${this.momentumRsiLength()}) range: ${this.momentumRsiLow()}–${this.momentumRsiHigh()}`,
          `MA filter: ${this.momentumFastMa()} / ${this.momentumSlowMa()}`,
          `Stoch %K/${this.momentumStochK()} %D/${this.momentumStochD()}`,
          `Exit ${this.momentumExitMinutes()} min before close`,
        ],
      },
      ema_crossover_rsi: {
        display: 'EMA Crossover + RSI',
        rules: [
          `Entry: EMA ${this.emaCrossoverFastPeriod()} crosses EMA ${this.emaCrossoverSlowPeriod()}`,
          `Filter: RSI(${this.emaCrossoverRsiPeriod()}) ${this.emaCrossoverRsiMin()}–${this.emaCrossoverRsiMax()}`,
          `Min gap: ${this.emaCrossoverMinGap()}`,
          `Exit: hold ${this.emaCrossoverExitBars()} bars`,
        ],
      },
      rsi_reversal: {
        display: 'RSI Reversal',
        rules: [
          `Buy reversal: RSI(${this.rsiReversalWindow()}) < ${this.rsiReversalOversold()}`,
          `Sell reversal: RSI(${this.rsiReversalWindow()}) > ${this.rsiReversalOverbought()}`,
        ],
      },
    };
    return meta[s] ?? { display: s, rules: [] };
  });

  // LEAN parity indicator
  isParityValidated = computed(() =>
    this.ticker().toUpperCase() === 'SPY' && this.strategyName() === 'ema_crossover_rsi'
  );
  parityReference = computed(() =>
    this.isParityValidated() ? 'SPY × EMA Crossover RSI (63 trades, bit-exact)' : ''
  );

  // Computed summary for collapsed advanced panel
  advancedSummary = computed(() => {
    const ts = this.timespan();
    const m = this.multiplier();
    const unit = ts === 'minute' ? 'm' : ts === 'hour' ? 'h' : 'd';
    const sess = this.session() === 'rth' ? 'RTH' : 'Extended';
    const fill = this.forwardFill() ? 'Fill' : 'No fill';
    const wu = this.warmup() ? 'Warmup' : 'No warmup';
    return `${m}${unit} · ${sess} · ${fill} · ${wu}`;
  });

  // State
  loading = signal(false);
  error = signal<string | null>(null);
  result = signal<BacktestResponse | null>(null);

  // Chart data from Python response
  chartBars = computed(() => this.result()?.chart_bars ?? []);
  chartIndicators = computed(() => this.result()?.chart_indicators ?? []);
  chartQuality = computed(() => this.result()?.quality ?? null);
  chartTrades = computed<BacktestTradeForChart[]>(() => this.result()?.trades ?? []);

  // Volume warning
  volumeWarning = signal('');

  // Computed: build parameters object for the Python endpoint
  private buildParameters(): Record<string, any> {
    switch (this.strategyName()) {
      case 'sma_crossover':
        return {
          ShortWindow: this.shortWindow(),
          LongWindow: this.longWindow(),
        };
      case 'rsi_mean_reversion':
        return {
          Window: this.rsiWindow(),
          Oversold: this.oversold(),
          Overbought: this.overbought(),
        };
      case 'momentum_rsi_stochastic':
        return {
          RsiLength: this.momentumRsiLength(),
          RsiLow: this.momentumRsiLow(),
          RsiHigh: this.momentumRsiHigh(),
          FastMa: this.momentumFastMa(),
          SlowMa: this.momentumSlowMa(),
          StochK: this.momentumStochK(),
          StochD: this.momentumStochD(),
          ExitMinutesBefore: this.momentumExitMinutes(),
        };
      case 'ema_crossover_rsi':
        return {
          strategy_name: 'ema_crossover_rsi',
          fast_ema_period: this.emaCrossoverFastPeriod(),
          slow_ema_period: this.emaCrossoverSlowPeriod(),
          rsi_period: this.emaCrossoverRsiPeriod(),
          adx_period: this.emaCrossoverAdxPeriod(),
          min_ema_gap: this.emaCrossoverMinGap(),
          rsi_min: this.emaCrossoverRsiMin(),
          rsi_max: this.emaCrossoverRsiMax(),
          exit_mode: 'fixed_bars',
          exit_bars: this.emaCrossoverExitBars(),
          direction: 'long',
        };
      case 'rsi_reversal':
        return {
          Window: this.rsiReversalWindow(),
          Oversold: this.rsiReversalOversold(),
          Overbought: this.rsiReversalOverbought(),
        };
      default:
        return {};
    }
  }

  // Replay timeframe for backward-compatible GraphQL calls
  private get replayTimeframe(): string {
    const ts = this.timespan();
    const m = this.multiplier();
    if (ts === 'hour') return m > 1 ? `${m}h` : '1h';
    if (ts === 'day') return '1D';
    return `${m}m`;
  }

  // Replay equity curve
  replayEquityCurve = computed<StockAggregate[]>(() => {
    const completed = this.replayStrategy.completedTrades();
    if (!completed.length) return [];
    return completed.map((t, i) => ({
      id: i,
      tickerId: 0,
      open: t.cumulativePnl,
      high: t.cumulativePnl,
      low: t.cumulativePnl,
      close: t.cumulativePnl,
      volume: 0,
      volumeWeightedAveragePrice: null,
      timestamp: t.exitTimestamp,
      timespan: 'trade',
      multiplier: 1,
      transactionCount: null,
    }));
  });

  constructor() {
    this.loadHolidays();
  }

  // ── Backtest ──

  async runBacktest(): Promise<void> {
    const dateError = validateDateRange(this.fromDate(), this.toDate());
    if (dateError) {
      this.error.set(dateError);
      return;
    }

    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    const body = {
      ticker: this.ticker().toUpperCase(),
      from_date: this.fromDate(),
      to_date: this.toDate(),
      timespan: this.timespan(),
      multiplier: this.multiplier(),
      session: this.session(),
      forward_fill: this.forwardFill(),
      warmup: this.warmup(),
      strategy_name: this.strategyName(),
      parameters: this.buildParameters(),
    };

    try {
      const res = await firstValueFrom(
        this.http.post<BacktestResponse>(
          `${environment.pythonServiceUrl}/api/backtest/run`,
          body,
        ),
      );

      if (!res.success) {
        this.error.set(res.error || 'Backtest failed');
      } else {
        this.result.set(res);
      }
    } catch (err: any) {
      this.error.set(err?.error?.detail || err?.message || 'Request failed');
    } finally {
      this.loading.set(false);
    }
  }

  // ── Holiday & date preset support ──

  private loadHolidays(): void {
    firstValueFrom(this.marketMonitor.getHolidays(20))
      .then(events => this.holidays.set(events))
      .catch(() => {});
  }

  getHolidayForDate(day: number, month: number, year: number): MarketHolidayEvent | null {
    const m = String(month + 1).padStart(2, '0');
    const d = String(day).padStart(2, '0');
    return this.holidayMap().get(`${year}-${m}-${d}`) ?? null;
  }

  getHolidayTooltip(holiday: MarketHolidayEvent): string {
    let text = holiday.name ?? 'Market Holiday';
    if (holiday.status === 'Early Close') {
      text += ' (Early Close)';
    } else if (holiday.status) {
      text += ` - ${holiday.status}`;
    }
    return text;
  }

  setPresetRange(daysBack: number): void {
    const to = StrategyLabComponent.getYesterday();
    const from = new Date(to);
    from.setDate(from.getDate() - daysBack);
    this.fromDateValue.set(from);
    this.toDateValue.set(to);
    this.activePreset.set(`${daysBack}d`);
  }

  setPresetMonths(months: number): void {
    const to = StrategyLabComponent.getYesterday();
    const from = new Date(to);
    from.setMonth(from.getMonth() - months);
    this.fromDateValue.set(from);
    this.toDateValue.set(to);
    const label = months >= 12 ? `${months / 12}y` : `${months}m`;
    this.activePreset.set(label);
  }

  // ── Reset form to defaults ──

  resetForm(): void {
    this.ticker.set('AAPL');
    this.fromDateValue.set(StrategyLabComponent.get30DaysAgo());
    this.toDateValue.set(StrategyLabComponent.getYesterday());
    this.timespan.set('minute');
    this.multiplier.set(5);
    this.session.set('rth');
    this.forwardFill.set(true);
    this.warmup.set(true);
    this.strategyName.set('sma_crossover');
    this.shortWindow.set(10);
    this.longWindow.set(30);
    this.activePreset.set(null);
    this.showAdvanced.set(false);
    this.showStrategyParams.set(false);
    this.showTradeLog.set(false);
    this.result.set(null);
    this.error.set(null);
  }

  // ── Ticker search ──

  searchTickers(event: AutoCompleteCompleteEvent): void {
    const query = event.query.toUpperCase();
    this.tickerSuggestions.set(
      this.commonTickers.filter(t => t.includes(query))
    );
  }

  // ── Helpers ──

  formatPct(val: number): string {
    return (val * 100).toFixed(3) + '%';
  }

  formatPrice(val: number): string {
    return val.toFixed(2);
  }

  formatTimestamp(iso: string): string {
    if (!iso) return '';
    const hasTimezone = iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso);
    const d = new Date(hasTimezone ? iso : iso + 'Z');
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
      timeZoneName: 'short',
    });
  }

  // ── ZIP download ──

  async generateZip(): Promise<void> {
    if (!this.result()) return;

    this.loading.set(true);
    this.error.set(null);

    const body = {
      ticker: this.ticker().toUpperCase(),
      from_date: this.fromDate(),
      to_date: this.toDate(),
      timespan: this.timespan(),
      multiplier: this.multiplier(),
      session: this.session(),
      forward_fill: this.forwardFill(),
      warmup: this.warmup(),
      strategy_name: this.strategyName(),
      parameters: this.buildParameters(),
    };

    try {
      const blob = await firstValueFrom(
        this.http.post(
          `${environment.pythonServiceUrl}/api/backtest/generate-zip`,
          body,
          { responseType: 'blob' },
        ),
      );

      const sessionLabel = this.session() === 'rth' ? 'rth' : 'ext';
      const r = this.result()!;
      const filename = `${r.ticker}_${r.strategy_name}_${r.timeframe}_${sessionLabel}_${this.fromDate()}_to_${this.toDate()}.zip`;
      this.downloadBlob(blob, filename);
    } catch (err: any) {
      this.error.set(err?.error?.detail || err?.message || 'ZIP generation failed');
    } finally {
      this.loading.set(false);
    }
  }

  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  // ── Replay ──

  loadReplayData(): void {
    const dateError = validateDateRange(this.fromDate(), this.toDate());
    if (dateError) {
      this.error.set(dateError);
      return;
    }

    this.replayLoading.set(true);
    this.error.set(null);
    this.replayDataLoaded.set(false);
    this.replayEngine.reset();
    this.replayIndicators.reset();
    this.replayStrategy.reset();

    const timespan = this.timespan();
    const multiplier = this.multiplier();
    this.marketDataService.getOrFetchStockAggregates(
      this.ticker().toUpperCase(),
      this.fromDate(),
      this.toDate(),
      timespan,
      multiplier,
    ).subscribe({
      next: (res) => {
        const aggregates = res.aggregates ?? [];
        if (aggregates.length === 0) {
          this.error.set('No data found for the specified range. Fetch data on the Market Data page first.');
          this.replayLoading.set(false);
          return;
        }

        this.replayEngine.load(aggregates);
        this.replayDataLoaded.set(true);
        this.replayLoading.set(false);
        this.loadReplayOverlays();
      },
      error: (err) => {
        this.error.set(err.message || 'Failed to load replay data');
        this.replayLoading.set(false);
      },
    });
  }

  private loadReplayOverlays(): void {
    this.replayOverlayLoading.set(true);
    const ticker = this.ticker().toUpperCase();
    const from = this.fromDate();
    const to = this.toDate();
    const timespan = this.timespan();
    const multiplier = this.multiplier();

    const indicatorList = this.strategyName() === 'momentum_rsi_stochastic'
      ? [
          { name: 'sma', window: this.momentumFastMa() },
          { name: 'sma', window: this.momentumSlowMa() },
        ]
      : [
          { name: 'sma', window: this.shortWindow() },
          { name: 'sma', window: this.longWindow() },
        ];

    const indicators$ = this.marketDataService.calculateIndicators(
      ticker, from, to,
      indicatorList,
      timespan, multiplier,
    );

    const parametersJson = JSON.stringify(this.buildParameters());
    const backtest$ = this.marketDataService.runBacktest(
      ticker, this.strategyName(), from, to,
      timespan, multiplier, parametersJson,
    );

    forkJoin({ indicators: indicators$, backtest: backtest$ }).subscribe({
      next: ({ indicators, backtest }) => {
        if (indicators.success && indicators.indicators) {
          this.replayIndicators.loadIndicators(indicators.indicators);
        }
        if (backtest.success && backtest.trades) {
          this.replayStrategy.loadTrades(backtest.trades);
        }
        this.replayOverlayLoading.set(false);
      },
      error: () => {
        this.replayOverlayLoading.set(false);
      },
    });
  }
}
