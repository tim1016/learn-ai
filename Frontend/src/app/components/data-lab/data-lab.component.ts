import {
  Component, signal, computed, effect, inject, viewChild,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SharedModule } from 'primeng/api';
import { Tooltip } from 'primeng/tooltip';
import { environment } from '../../../environments/environment';
import { DataLabChartComponent, ChartIndicatorEntry } from './data-lab-chart/data-lab-chart.component';
import { DataLabSessionService, DataLabSessionSummary, DataLabSessionChartSnapshot } from '../../services/data-lab-session.service';
import { MarketMonitorService } from '../../services/market-monitor.service';
import { RunSessionService } from '../../services/run-session.service';
import { MarketHolidayEvent } from '../../models/market-monitor';
import { IndicatorTooltipComponent } from '../../shared/indicator-tooltip/indicator-tooltip.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import {
  TickerRangePickerComponent,
  type AdvisoryAction,
  type AvailabilityCell,
  type Resolution,
  type TickerOption,
  type TickerRange,
} from '../../shared/ticker-range-picker';
import { TICKER_POOL, RECENT_TICKERS } from '../../shared/ticker-catalog';
import {
  getDisabledHolidayDates,
  buildHolidayMap,
  getMinAllowedDate,
  validateDateRange,
} from '../../utils/date-validation';

/**
 * User-facing bar timeframes. Each entry bundles the Polygon ``timespan``
 * + ``multiplier`` pair so the UI can present a single "Bar timeframe"
 * dropdown without leaking the multiplier-magic to the user.
 */
interface BarTimeframeOption {
  /** The dropdown value (also the displayed label suffix). */
  value: string;
  label: string;
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
}

const BAR_TIMEFRAMES: readonly BarTimeframeOption[] = [
  { value: '1m', label: '1 min', timespan: 'minute', multiplier: 1 },
  { value: '5m', label: '5 min', timespan: 'minute', multiplier: 5 },
  { value: '15m', label: '15 min', timespan: 'minute', multiplier: 15 },
  { value: '30m', label: '30 min', timespan: 'minute', multiplier: 30 },
  { value: '1h', label: '1 hour', timespan: 'hour', multiplier: 1 },
  { value: '4h', label: '4 hours', timespan: 'hour', multiplier: 4 },
  { value: '1d', label: '1 day', timespan: 'day', multiplier: 1 },
];

/**
 * Auto bar-timeframe heuristic — picks a Polygon bar resolution from the
 * calendar-day span of the range so a fetch returns in a reasonable time
 * without asking the user to think in "bars per day". Locked by product
 * on 2026-04-24:
 *
 *   ≤ 5 days       → 1-minute
 *   5–30 days      → 5-minute
 *   30–120 days    → 15-minute
 *   120–365 days   → 1-hour
 *   > 365 days     → 1-hour (Polygon Starter's cap is 2 years anyway)
 */
export function pickAutoBarTimeframe(spanDays: number): string {
  if (spanDays <= 5) return '1m';
  if (spanDays <= 30) return '5m';
  if (spanDays <= 120) return '15m';
  return '1h';
}

interface ParamConfig {
  name: string;
  type: string;
  default: number;
  min: number;
  max: number;
  description: string;
}

interface IndicatorInfo {
  name: string;
  category: string;
  description: string;
  configurable_params: ParamConfig[];
}

interface IndicatorEntry {
  name: string;
  params: Record<string, number>;
}

interface AvailableResponse {
  success: boolean;
  categories: Record<string, IndicatorInfo[]>;
  total: number;
}

interface QualityReportStep {
  order: number;
  name: string;
  library: string;
  description: string;
  bars_before: number;
  bars_after: number;
  bars_removed: number;
}

interface QualityReportSummary {
  total_bars: number;
  trading_days: number;
  zero_volume_bars: number;
  flat_bars_ohlc_equal: number;
  fractional_volume_bars: number;
  vwap_above_high: number;
  vwap_below_low: number;
  ohlc_violations: number;
  duplicate_timestamps: number;
  weekend_bars: number;
  intraday_gaps: number;
}

interface QualityReportResponse {
  success: boolean;
  ticker: string;
  from_date: string;
  to_date: string;
  raw_summary: QualityReportSummary;
  clean_summary: QualityReportSummary;
  steps: QualityReportStep[];
}

interface CategoryData {
  name: string;
  indicators: IndicatorInfo[];
}

// ── Indicator runtime metadata for tooltips & warnings ─────────
interface IndicatorMeta {
  goodTimeframes: string[];
  poorTimeframes: string[];
  volumeDependent: boolean;
  tooltipSummary: string;
}

const VOLUME_DEPENDENT_INDICATORS = ['vwap', 'ad', 'cmf', 'mfi', 'obv'];

const INDICATOR_META: Record<string, IndicatorMeta> = {
  // Overlay
  ema:        { goodTimeframes: ['1m','5m','15m','30m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Exponential Moving Average. Weights recent prices more heavily. Use multiple lengths for crossover signals and ribbon analysis.' },
  sma:        { goodTimeframes: ['1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Simple Moving Average. Arithmetic mean of last n closes. Price above SMA = bullish, below = bearish.' },
  dema:       { goodTimeframes: ['5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Double EMA. Reduces lag vs standard EMA. Good for catching early trend entries.' },
  tema:       { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: ['1m','5m'], volumeDependent: false, tooltipSummary: 'Triple EMA. Further lag reduction. Better for early reversals but can be noisy on very short timeframes.' },
  wma:        { goodTimeframes: ['1m','5m','15m','1h','4h'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Weighted Moving Average. Heavier weight on recent bars. Good for short-term trend tracking and crossover systems.' },
  hma:        { goodTimeframes: ['4h','1D'], poorTimeframes: ['1m','5m'], tooltipSummary: 'Hull Moving Average. Very smooth with minimal lag. Can be choppy on low timeframes.', volumeDependent: false },
  kama:       { goodTimeframes: ['1m','5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Kaufman Adaptive MA. Adapts smoothing to market efficiency. Smooth in chop, fast in trends.' },
  zlma:       { goodTimeframes: ['1m','5m','15m','1h'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Zero Lag MA. Compensates for EMA lag. Faster signals but more false positives in sideways markets.' },
  rma:        { goodTimeframes: ['1m','5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Wilder Smoothing (RMA). Used internally by ATR, RSI, ADX. Slightly slower than EMA.' },
  alma:       { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Arnaud Legoux MA. Gaussian-weighted smoothing. Very smooth and low-lag for medium-term trends.' },
  bbands:     { goodTimeframes: ['5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Bollinger Bands. Volatility bands around SMA. Band squeeze = breakout pending. %B shows price position within bands.' },
  supertrend: { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: ['1m'], volumeDependent: false, tooltipSummary: 'ATR-based trend follower. Direction flips between support/resistance. Higher multiplier = fewer whipsaws.' },
  vwap:       { goodTimeframes: ['1m','5m','15m','30m'], poorTimeframes: ['1D'], volumeDependent: true, tooltipSummary: 'Volume-weighted average price. Intraday fair value anchor. Resets each session. Requires reliable volume.' },
  psar:       { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Parabolic SAR. Trailing stop-and-reverse. Dots below price = uptrend, above = downtrend.' },
  kc:         { goodTimeframes: ['5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Keltner Channel. ATR-based volatility bands. Smoother than Bollinger. Break above upper = bullish expansion.' },
  donchian:   { goodTimeframes: ['1h','4h','1D'], poorTimeframes: ['1m','5m'], volumeDependent: false, tooltipSummary: 'Donchian Channel. Highest high / lowest low over n bars. Used in Turtle Trading breakout systems.' },
  // Sub-panel
  macd:       { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: ['1m'], volumeDependent: false, tooltipSummary: 'MACD. Momentum from EMA convergence/divergence. Signal crossovers and histogram for entry timing.' },
  rsi:        { goodTimeframes: ['5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'RSI. Momentum oscillator. Above 70 = overbought, below 30 = oversold. Divergence signals reversals.' },
  adx:        { goodTimeframes: ['1h','4h','1D'], poorTimeframes: ['1m'], volumeDependent: false, tooltipSummary: 'ADX. Measures trend strength (not direction). Above 25 = strong trend, below 20 = range-bound.' },
  atr:        { goodTimeframes: ['1m','5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'ATR. Volatility measure. Rising ATR = expanding vol. Used for position sizing and stop placement.' },
  stoch:      { goodTimeframes: ['5m','15m','1h','4h'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Stochastic. Close position in high-low range. K/D above 80 = overbought, below 20 = oversold.' },
  stochrsi:   { goodTimeframes: ['1m','5m','15m','1h','4h'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Stochastic RSI. Stochastic applied to RSI — faster oscillator. Near 0 = oversold, near 1 = overbought.' },
  obv:        { goodTimeframes: ['1h','4h','1D'], poorTimeframes: [], volumeDependent: true, tooltipSummary: 'On Balance Volume. Cumulative volume flow. Rising OBV confirms uptrend. Divergence = potential reversal.' },
  cci:        { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'CCI. Deviation from mean. Above +100 = overbought, below -100 = oversold.' },
  willr:      { goodTimeframes: ['1m','5m','15m','1h','4h'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Williams %R. Close vs high-low range. -20 to 0 = overbought, -80 to -100 = oversold.' },
  roc:        { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Rate of Change. Percentage price change over n bars. Above 0 = bullish momentum.' },
  mom:        { goodTimeframes: ['1m','5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Momentum. Absolute price change over n periods. Best paired with normalization or other confirmation.' },
  natr:       { goodTimeframes: ['1m','5m','15m','1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'Normalized ATR. ATR as percentage of price. Comparable across instruments for risk sizing.' },
  ad:         { goodTimeframes: ['4h','1D'], poorTimeframes: ['1m','5m'], volumeDependent: true, tooltipSummary: 'A/D Line. Buying vs selling pressure via close position + volume. Divergence with price = distribution.' },
  cmf:        { goodTimeframes: ['1D'], poorTimeframes: [], volumeDependent: true, tooltipSummary: 'Chaikin Money Flow. Accumulation/distribution over n bars. Positive = buying, negative = selling pressure.' },
  mfi:        { goodTimeframes: ['4h','1D'], poorTimeframes: ['1m','5m'], volumeDependent: true, tooltipSummary: 'Money Flow Index. Volume-weighted RSI. Above 80 = overbought, below 20 = oversold.' },
  tsi:        { goodTimeframes: ['1h','4h','1D'], poorTimeframes: [], volumeDependent: false, tooltipSummary: 'True Strength Index. Smooth momentum oscillator. Above 0 = bullish. Signal line crossovers for entries.' },
  fisher:     { goodTimeframes: ['15m','1h','4h'], poorTimeframes: ['1m','5m'], volumeDependent: false, tooltipSummary: 'Fisher Transform. Gaussian-normalized price. Crossing signal = reversal. Too noisy below 15m.' },
  squeeze:    { goodTimeframes: ['15m','1h','4h','1D'], poorTimeframes: ['1m','5m'], volumeDependent: false, tooltipSummary: 'Volatility Squeeze. BB inside KC = compression. Squeeze release signals breakout.' },
};

// Default indicators pre-selected on load
const DEFAULT_ENTRIES: IndicatorEntry[] = [
  { name: 'ema', params: { length: 5 } },
  { name: 'ema', params: { length: 10 } },
  { name: 'ema', params: { length: 20 } },
  { name: 'ema', params: { length: 30 } },
  { name: 'ema', params: { length: 40 } },
  { name: 'ema', params: { length: 50 } },
  { name: 'ema', params: { length: 100 } },
  { name: 'ema', params: { length: 200 } },
  { name: 'bbands', params: { length: 20, std: 2.0 } },
  { name: 'supertrend', params: { length: 10, multiplier: 3.0 } },
  { name: 'macd', params: { fast: 12, slow: 26, signal: 9 } },
  { name: 'rsi', params: { length: 14 } },
  { name: 'adx', params: { length: 14 } },
];

@Component({
  selector: 'app-data-lab',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, DataLabChartComponent, SharedModule, Tooltip, IndicatorTooltipComponent, PageHeaderComponent, TickerRangePickerComponent],
  templateUrl: './data-lab.component.html',
  styleUrls: ['./data-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabComponent {
  private http = inject(HttpClient);
  private sessionService = inject(DataLabSessionService);
  private marketMonitor = inject(MarketMonitorService);
  /** Drives the streaming Fetch & bundle progress surface (states B/C/D/E). */
  readonly runSession = inject(RunSessionService);

  /** Reference to the chart child so we can call loadCachedData(). */
  chartComponent = viewChild<DataLabChartComponent>('chartComponent');

  // ── Session management state ──────────────────────────────
  savedSessions = signal<DataLabSessionSummary[]>([]);
  activeSessionId = signal<string | null>(null);
  sessionPanelOpen = signal(false);
  savingSession = signal(false);
  sessionName = signal('');
  renamingSessionId = signal<string | null>(null);
  renameValue = signal('');

  /** The latest chart snapshot received from the chart component. */
  private latestChartSnapshot = signal<DataLabSessionChartSnapshot | null>(null);

  ticker = signal('SPY');

  // Date state: PrimeNG DatePicker binds to Date objects
  private static getYesterday(): Date {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    d.setHours(0, 0, 0, 0);
    return d;
  }
  private static get30DaysAgo(): Date {
    const d = DataLabComponent.getYesterday();
    d.setDate(d.getDate() - 30);
    return d;
  }
  private static formatDate(d: Date): string {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  private static parseDate(dateStr: string): Date {
    const [y, m, d] = dateStr.split('-').map(Number);
    const date = new Date(y, m - 1, d, 0, 0, 0, 0);
    return date;
  }

  fromDateValue = signal<Date>(DataLabComponent.get30DaysAgo());
  toDateValue = signal<Date>(DataLabComponent.getYesterday());
  fromDate = computed(() => DataLabComponent.formatDate(this.fromDateValue()));
  toDate = computed(() => DataLabComponent.formatDate(this.toDateValue()));

  // Calendar constraints
  holidays = signal<MarketHolidayEvent[]>([]);
  disabledDates = computed(() => getDisabledHolidayDates(this.holidays()));
  holidayMap = computed(() => buildHolidayMap(this.holidays()));
  disabledDays: number[] = [0, 6];
  minDate = new Date(getMinAllowedDate() + 'T00:00:00');
  maxDate = DataLabComponent.getYesterday();

  // Date range validation
  dateRangeWarning = computed(() => validateDateRange(this.fromDate(), this.toDate()));

  session = signal<'rth' | 'extended'>('rth');
  forwardFill = signal(true);
  /** Adjust for stock splits (Polygon's built-in adjusted=true). Reliable. */
  adjustForSplits = signal(true);
  /**
   * Adjust for dividends. Polygon does NOT do this natively — its
   * ``adjusted=true`` only covers splits. When this is on we fetch the
   * dividend reference file and subtract each dividend from bars dated
   * before its ex-date on the server, producing TradingView-style
   * dividend-adjusted prices. See ``docs/tv-polygon-validation-gotchas.md`` §1.
   */
  adjustForDividends = signal(false);
  warmup = signal(true);
  computeAllIndicators = signal(false);
  /** Auto-pick bar timeframe from the date range so fetch time stays sane. */
  autoBarTimeframe = signal(true);
  /**
   * Auto-split the request into chunks that stay under Polygon's 50k-bar
   * per-request cap and are paced to the Starter plan's 5 req/min limit.
   */
  autoChunk = signal(true);
  /** When on, Fetch Data also builds & downloads the dataset ZIP. */
  alsoGenerateZip = signal(false);
  timespan = signal<'second' | 'minute' | 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year'>('minute');
  multiplier = signal(1);
  // Polygon /v2/aggs passthrough params
  sort = signal<'asc' | 'desc'>('asc');
  polygonLimit = signal(50000);

  // ── Shared ticker-range picker wiring ─────────────────────
  readonly tickerPool: readonly TickerOption[] = TICKER_POOL;
  readonly recentTickers: readonly string[] = RECENT_TICKERS;

  private static timespanToResolution(
    t: 'second' | 'minute' | 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year',
  ): Resolution {
    if (t === 'hour') return 'hour';
    if (t === 'second' || t === 'minute') return 'minute';
    return 'daily';
  }

  /** Writable picker state — two-way-bound so user edits survive change
   *  detection. An effect below propagates ``rangeState`` into the
   *  legacy ticker / fromDate / toDate signals that the rest of the
   *  component reads from. */
  readonly rangeState = signal<TickerRange>({
    symbol: 'SPY',
    from: DataLabComponent.formatDate(DataLabComponent.get30DaysAgo()),
    to: DataLabComponent.formatDate(DataLabComponent.getYesterday()),
    resolution: 'minute',
    autoFetch: true,
  });

  /** Empty per-day cells for now — Data Lab doesn't yet fetch an
   *  availability report. Advisories still fire on range-based signals
   *  ("minute × 90+ days → switch to hour"). */
  readonly pickerAvailability: readonly AvailabilityCell[] = [];

  onPickerAdvisoryAction(_action: AdvisoryAction): void {
    // No side-effects wired yet — the picker already patches
    // ``rangeState`` on action; this would be where we triggered a
    // fetch or a refetch.
  }

  // ── Bar timeframe (hides timespan × multiplier magic) ─────
  readonly barTimeframes = BAR_TIMEFRAMES;

  /** Which bar-timeframe option is currently selected, derived from the
   *  underlying ``timespan`` + ``multiplier`` signals. Falls back to
   *  "custom" when the combination doesn't match a preset so the user
   *  can still see what's configured via the advanced fields. */
  readonly activeBarTimeframe = computed<string>(() => {
    const t = this.timespan();
    const m = this.multiplier();
    const match = BAR_TIMEFRAMES.find(
      (b) => b.timespan === t && b.multiplier === m,
    );
    return match ? match.value : 'custom';
  });

  setBarTimeframe(value: string): void {
    const entry = BAR_TIMEFRAMES.find((b) => b.value === value);
    if (!entry) return;
    this.timespan.set(entry.timespan);
    this.multiplier.set(entry.multiplier);
  }

  /** Calendar-day span of the current from/to. */
  readonly spanCalendarDays = computed<number>(() => {
    const from = new Date(this.fromDate()).getTime();
    const to = new Date(this.toDate()).getTime();
    if (Number.isNaN(from) || Number.isNaN(to) || to < from) return 0;
    return Math.round((to - from) / 86_400_000);
  });

  /** Human-friendly explanation of what Auto picked and why. */
  readonly autoBarTimeframeReadout = computed<string>(() => {
    const days = this.spanCalendarDays();
    const picked = pickAutoBarTimeframe(days);
    const label = BAR_TIMEFRAMES.find((b) => b.value === picked)?.label ?? picked;
    return `${label} bars for ${days}-day range`;
  });

  /**
   * Expected number of bars for the current (range × resolution × session)
   * combination. Used to size the Auto Chunk readout. Uses the same
   * bars-per-day constants as the Python chunker so the two agree.
   */
  private static readonly BARS_PER_DAY: Record<string, number> = {
    second: 27_000,
    minute: 450,
    hour: 24,
    day: 1,
    week: 1,
    month: 1,
    quarter: 1,
    year: 1,
  };

  readonly expectedBarCount = computed<number>(() => {
    const days = this.spanCalendarDays();
    // Weekends-only approximation: ~5/7ths of calendar days are trading days.
    const tradingDays = Math.ceil((days * 5) / 7);
    const bpd =
      (DataLabComponent.BARS_PER_DAY[this.timespan()] ?? 450) /
      Math.max(1, this.multiplier());
    const rthAdjust = this.session() === 'rth' && this.timespan() === 'minute' ? 390 / 450 : 1;
    return Math.max(1, Math.ceil(tradingDays * bpd * rthAdjust));
  });

  /**
   * Layman-friendly readout for Auto Chunk — tells the user how many
   * requests the fetch will issue and how long it will take given
   * Polygon Starter's 5 req/min cap. Matches the design brief's wording
   * convention: prefer "requests" / "slot" / "paced" over "chunk" /
   * "rate-limit" / "backoff".
   */
  readonly autoChunkReadout = computed<string>(() => {
    const bars = this.expectedBarCount();
    const chunks = Math.max(1, Math.ceil(bars / 50_000));
    if (!this.autoChunk()) {
      return `Manual: ${this.polygonLimit().toLocaleString()} bars per request.`;
    }
    if (chunks === 1) {
      return `1 request · ~${bars.toLocaleString()} bars · fits in a single slot.`;
    }
    // Starter plan: 5 req/min → 12 s per request once the first 5 are spent.
    const paced = Math.max(0, chunks - 5);
    const approxSec = Math.round(paced * 12 + Math.min(chunks, 5) * 1);
    return `Plan runs ${chunks} requests · ~${bars.toLocaleString()} bars · ~${approxSec}s on your 5-req/min slot.`;
  });

  loading = signal(false);
  loadingIndicators = signal(false);
  loadingValidation = signal(false);
  error = signal('');
  progress = signal('');

  // ── Options companion config ───────────────────────────────
  optionsCompanionEnabled = signal(false);
  optionsStrikesEachSide = signal(5);
  optionsIncludeCalls = signal(true);
  optionsIncludePuts = signal(true);
  optionsExpiryMode = signal<'same_day' | 'nearest_within_days'>('same_day');
  optionsMaxDte = signal(7);
  optIncludeOhlcv = signal(true);
  optIncludeVwap = signal(true);
  optIncludeTransactions = signal(true);
  optIncludeOi = signal(false);
  optIncludeIv = signal(true);
  optIncludeDelta = signal(true);
  optIncludeGamma = signal(true);
  optIncludeTheta = signal(true);
  optIncludeVega = signal(true);
  optIncludeRho = signal(false);
  optRiskFreeRate = signal(0.05);
  optDividendYield = signal(0.0);

  // Tickers with verified daily (Mon-Fri) expiries as of 2026
  readonly DAILY_EXPIRY_TICKERS = ['SPY','QQQ','IWM','SPX','XSP','NDX','XND','DIA','VIX'];
  tickerSupportsDaily = computed(() =>
    this.DAILY_EXPIRY_TICKERS.includes(this.ticker().toUpperCase())
  );

  /**
   * Low-resolution warning for the options companion.
   *
   * - `same_day` + `day` resolution → each 0DTE contract produces a single row (degenerate).
   * - `same_day` + `hour` resolution → each 0DTE contract produces ~6-7 rows (sparse).
   * - `nearest_within_days` + `day` resolution → each contract produces up to `max_dte` rows.
   * Returns an explanatory string when the combination is degenerate, else empty.
   */
  optionsResolutionWarning = computed(() => {
    if (!this.optionsCompanionEnabled()) return '';
    const ts = this.timespan();
    const mult = this.multiplier();
    const mode = this.optionsExpiryMode();
    if (ts === 'day') {
      if (mode === 'same_day') {
        return 'At day resolution with Same-day (0DTE) expiry, each contract produces exactly 1 row. You probably want minute or hour resolution for intraday IV/Greeks.';
      }
      return `At day resolution, each selected contract will produce up to ${this.optionsMaxDte()} daily rows.`;
    }
    if (ts === 'hour' && mode === 'same_day') {
      return `At ${mult}-hour resolution with Same-day (0DTE) expiry, each contract produces ~${Math.max(1, Math.floor(7 / Math.max(mult, 1)))} rows — limited intraday granularity.`;
    }
    return '';
  });

  // ── Data Quality Report panel state ────────────────────────
  includeQualityReportInZip = signal(false);
  qualityReportLoading = signal(false);
  qualityReportResult = signal<QualityReportResponse | null>(null);
  qualityReportError = signal('');

  // ── Polygon reference-endpoint companion toggles ───────────
  includeSplits = signal(false);
  includeDividends = signal(false);
  includeTickerOverview = signal(false);
  includeNews = signal(false);
  includeFinancials = signal(false);
  includeStockTrades = signal(false);
  includeStockQuotes = signal(false);

  // Validation report state
  validationReport = signal('');
  ourCsvFile = signal<File | null>(null);
  tvCsvFile = signal<File | null>(null);

  categories = signal<CategoryData[]>([]);
  indicatorMap = signal<Record<string, IndicatorInfo>>({});

  // Active indicator entries (each with name + params)
  entries = signal<IndicatorEntry[]>([...DEFAULT_ENTRIES]);

  // Which indicator we're configuring (for the add form)
  addingIndicator = signal<string>('');
  addParams = signal<Record<string, number>>({});

  expandedCategories = signal<Set<string>>(new Set());

  // Volume warning — set after chart data loads
  volumeWarning = signal('');

  // Selected indicator names (unique set for checkbox state).
  // Was a getter that allocated a new Set on every template read — the
  // category grid called it ~80 times per render, and each keystroke in
  // a param input triggered ~1,040 Set allocations (audit § 4.3).
  // As a computed it memoizes until entries() changes.
  selectedNames = computed<Set<string>>(() => new Set(this.entries().map(e => e.name)));

  entryCount = computed(() => this.entries().length);

  // Chart-compatible indicator entries (same shape, typed for chart component)
  chartIndicators = computed<ChartIndicatorEntry[]>(() =>
    this.entries().map(e => ({ name: e.name, params: e.params }))
  );

  // Current timeframe key for warning logic (e.g. "1m", "5m", "1h", "1D")
  currentTimeframeKey = computed(() => {
    const m = this.multiplier();
    const t = this.timespan();
    const suffix = t === 'day' ? 'D' : t[0];
    return `${m}${suffix}`;
  });

  // Timeframe warnings per entry index
  timeframeWarnings = computed<Record<number, string>>(() => {
    const key = this.currentTimeframeKey();
    const warnings: Record<number, string> = {};
    this.entries().forEach((entry, i) => {
      const meta = INDICATOR_META[entry.name];
      if (!meta) return;
      if (meta.poorTimeframes.includes(key)) {
        warnings[i] = `${entry.name} is not recommended for ${key} timeframe`;
      } else if (meta.goodTimeframes.length > 0 && !meta.goodTimeframes.includes(key)) {
        warnings[i] = `${entry.name} works best on: ${meta.goodTimeframes.join(', ')}`;
      }
    });
    return warnings;
  });

  // Estimated output columns
  estimatedColumns = computed(() => {
    const base = ['unix_ts', 'iso_time', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'transactions'];
    const indicatorCols: string[] = [];
    for (const entry of this.entries()) {
      const info = this.indicatorMap()[entry.name];
      // Estimate columns based on known multi-column indicators
      const paramStr = Object.entries(entry.params).map(([k, v]) => `${v}`).join('_');
      const suffix = paramStr ? `_${paramStr}` : '';
      const multiCol: Record<string, string[]> = {
        bbands: [`bbl${suffix}`, `bbm${suffix}`, `bbu${suffix}`, `bbb${suffix}`, `bbp${suffix}`],
        macd: [`macd${suffix}`, `macdh${suffix}`, `macds${suffix}`],
        supertrend: [`supert${suffix}`, `supertd${suffix}`, `supertl${suffix}`, `superts${suffix}`],
        stoch: [`stochk${suffix}`, `stochd${suffix}`],
        aroon: [`aroond${suffix}`, `aroonu${suffix}`, `aroonosc${suffix}`],
        kc: [`kcl${suffix}`, `kcb${suffix}`, `kcu${suffix}`],
        donchian: [`dcl${suffix}`, `dcm${suffix}`, `dcu${suffix}`],
        adx: [`adx${suffix}`, `dmp${suffix}`, `dmn${suffix}`],
      };
      if (multiCol[entry.name]) {
        indicatorCols.push(...multiCol[entry.name]);
      } else {
        indicatorCols.push(`${entry.name}${suffix}`);
      }
    }
    return [...base, ...indicatorCols];
  });

  constructor() {
    // Propagate picker state into the legacy ticker / date / timespan
    // signals. Writing to ``rangeState`` is the only way users change
    // (symbol, from, to, resolution); everything else below the picker
    // reads from the old signals without needing to know the picker
    // exists.
    effect(
      () => {
        const v = this.rangeState();
        if (this.ticker() !== v.symbol) this.ticker.set(v.symbol);
        const fromIso = DataLabComponent.formatDate(this.fromDateValue());
        const toIso = DataLabComponent.formatDate(this.toDateValue());
        if (fromIso !== v.from) {
          this.fromDateValue.set(DataLabComponent.parseDate(v.from));
        }
        if (toIso !== v.to) {
          this.toDateValue.set(DataLabComponent.parseDate(v.to));
        }
        // Only overwrite timespan if the picker's resolution doesn't
        // already match — Data Lab's bar-timeframe dropdown writes the
        // multiplier too, and the picker shouldn't undo that.
        const expected = DataLabComponent.timespanToResolution(this.timespan());
        if (v.resolution !== expected) {
          this.timespan.set(v.resolution === 'daily' ? 'day' : v.resolution);
          // Reset multiplier so the bar-timeframe dropdown matches a
          // preset after a coarse resolution change via the picker.
          this.multiplier.set(1);
        }
      },
      { allowSignalWrites: true },
    );

    // Auto-resolution: when the toggle is on, derive bar timeframe from
    // the current date range. The user can still click the dropdown —
    // turning Auto off reveals whatever was last auto-picked and hands
    // the control back.
    effect(
      () => {
        if (!this.autoBarTimeframe()) return;
        const days = this.spanCalendarDays();
        const picked = pickAutoBarTimeframe(days);
        if (this.activeBarTimeframe() !== picked) {
          this.setBarTimeframe(picked);
        }
      },
      { allowSignalWrites: true },
    );

    // Note: dividend adjustment requires the ``dividends`` companion file,
    // but we deliberately do NOT auto-enable it. The brief calls out
    // "dependencies are visible, not policed" — we surface a callout next
    // to the checkbox with explicit [Enable companion] / [Keep disabled]
    // actions, leaving the user in control. See data-lab.component.html
    // for the dependency-callout that handles this.

    this.loadAvailableIndicators();
    this.refreshSessionList();
    this.loadHolidays();
  }

  /**
   * Unified primary action.
   *
   * Always kicks off the chart's own fetch so the preview lights up. When
   * the "Also generate" checkbox is on, additionally drives the streaming
   * dataset pipeline via :class:`RunSessionService` — that's the source
   * of the chunk-level progress surface (states B/C/D/E in the design
   * brief). The two paths run in parallel today (chart fetch + run-stream
   * fetch); they both hit Polygon so we double the request count, but
   * the chart endpoint isn't yet on the streaming pipeline.
   */
  async fetchAndMaybeZip(): Promise<void> {
    const chart = this.chartComponent();
    if (chart) chart.fetchData();
    if (this.alsoGenerateZip()) {
      await this.runSession.start(this._buildGenerateZipPayload());
    }
  }

  /** Index of the chunk currently being fetched, for the run-card heading.
   *  Falls back to the highest done index + 1 if no chunk is mid-flight. */
  readonly runChunkInProgressIndex = computed<number>(() => {
    const chunks = this.runSession.chunks();
    const fetching = chunks.find((c) => c.status === 'fetching');
    if (fetching) return fetching.index;
    const done = chunks.filter((c) => c.status === 'done').length;
    return Math.min(done + 1, chunks.length || 1);
  });

  /** Aggregate progress as a 0–100 integer, for ARIA progressbar + label. */
  readonly runProgressPercent = computed<number>(() =>
    Math.round(this.runSession.progressFraction() * 100),
  );

  /** Hand-off to the streaming pipeline — the same payload the legacy
   *  ``generateZip`` endpoint expects. */
  private _buildGenerateZipPayload(): Record<string, unknown> {
    const optionsConfig = this.optionsCompanionEnabled()
      ? {
          enabled: true,
          strikes_each_side: this.optionsStrikesEachSide(),
          include_calls: this.optionsIncludeCalls(),
          include_puts: this.optionsIncludePuts(),
          expiry_mode: this.optionsExpiryMode(),
          max_dte: this.optionsMaxDte(),
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
          risk_free_rate: this.optRiskFreeRate(),
          dividend_yield: this.optDividendYield(),
        }
      : null;
    return {
      ticker: this.ticker(),
      from_date: this.fromDate(),
      to_date: this.toDate(),
      indicator_entries: this.entries(),
      session: this.session(),
      forward_fill: this.forwardFill(),
      adjusted: this.adjustForSplits(),
      adjust_for_dividends: this.adjustForDividends(),
      warmup: this.warmup(),
      timespan: this.timespan(),
      multiplier: this.multiplier(),
      sort: this.sort(),
      limit: this.polygonLimit(),
      options_companion: optionsConfig,
      include_quality_report: this.includeQualityReportInZip(),
      include_splits: this.includeSplits(),
      include_dividends: this.includeDividends(),
      include_ticker_overview: this.includeTickerOverview(),
      include_news: this.includeNews(),
      include_financials: this.includeFinancials(),
      include_trades: this.includeStockTrades(),
      include_quotes: this.includeStockQuotes(),
    };
  }

  // ── Session management ─────────────────────────────────────

  async refreshSessionList(): Promise<void> {
    this.savedSessions.set(await this.sessionService.listSessions());
  }

  toggleSessionPanel(): void {
    this.sessionPanelOpen.update(v => !v);
    if (this.sessionPanelOpen()) this.refreshSessionList();
  }

  /**
   * Called by the chart component's (dataLoaded) event.
   * Captures the chart payload so the next "Save" includes it.
   */
  onChartDataLoaded(event: {
    bars: any[];
    indicators: any[];
    quality: any;
    allowedTimeframes: string[];
    estimatedBarsPerTimeframe: Record<string, number>;
    recommendedTimeframe: string;
    visibleIndicatorIds: string[];
    timeframe: string;
  }): void {
    this.latestChartSnapshot.set({
      timeframe: event.timeframe,
      bars: event.bars,
      indicators: event.indicators,
      quality: event.quality,
      allowedTimeframes: event.allowedTimeframes,
      estimatedBarsPerTimeframe: event.estimatedBarsPerTimeframe,
      recommendedTimeframe: event.recommendedTimeframe,
      visibleIndicatorIds: event.visibleIndicatorIds,
    });

    // Auto-update snapshot if this is an active (already-saved) session
    const activeId = this.activeSessionId();
    if (activeId) {
      this.sessionService.updateChartSnapshot(activeId, this.latestChartSnapshot()!);
    }

    // Volume dependency check
    const bars = event.bars as { v: number }[];
    const zeroVolBars = bars.filter(b => !b.v || b.v === 0).length;
    const zeroVolPct = bars.length > 0 ? (zeroVolBars / bars.length) * 100 : 0;
    const activeVolDep = this.entries().filter(e => VOLUME_DEPENDENT_INDICATORS.includes(e.name));

    if (zeroVolPct > 10 && activeVolDep.length > 0) {
      const names = [...new Set(activeVolDep.map(e => e.name))].join(', ');
      this.volumeWarning.set(
        `${zeroVolPct.toFixed(0)}% of bars have zero/missing volume. Volume-dependent indicators (${names}) may be unreliable.`
      );
    } else {
      this.volumeWarning.set('');
    }
  }

  async saveSession(): Promise<void> {
    this.savingSession.set(true);
    try {
      const config = {
        ticker: this.ticker(),
        fromDate: this.fromDate(),
        toDate: this.toDate(),
        session: this.session(),
        forwardFill: this.forwardFill(),
        adjusted: this.adjustForSplits(),
        entries: this.entries(),
      };

      const activeId = this.activeSessionId();
      if (activeId) {
        // Update existing session
        await this.sessionService.updateSession(
          activeId,
          config,
          this.latestChartSnapshot(),
          this.sessionName() || config.ticker
        );
      } else {
        // Create new session
        const newId = await this.sessionService.saveSession(
          config,
          this.latestChartSnapshot(),
          this.sessionName() || undefined
        );
        if (newId) this.activeSessionId.set(newId);
      }

      await this.refreshSessionList();
    } finally {
      this.savingSession.set(false);
    }
  }

  async saveAsNewSession(): Promise<void> {
    this.activeSessionId.set(null); // Force new
    await this.saveSession();
  }

  async loadSession(id: string): Promise<void> {
    const session = await this.sessionService.getSession(id);
    if (!session) return;

    // Restore configuration
    this.ticker.set(session.config.ticker);
    this.fromDateValue.set(DataLabComponent.parseDate(session.config.fromDate));
    this.toDateValue.set(DataLabComponent.parseDate(session.config.toDate));
    this.session.set(session.config.session);
    this.forwardFill.set(session.config.forwardFill);
    this.adjustForSplits.set(session.config.adjusted ?? true);
    this.adjustForDividends.set(false);
    this.entries.set([...session.config.entries]);
    this.activeSessionId.set(session.id);
    this.sessionName.set(session.name);

    // Restore chart data from snapshot (no API call)
    if (session.chartSnapshot) {
      this.latestChartSnapshot.set(session.chartSnapshot);

      // Give Angular a tick to propagate input changes to the chart child
      setTimeout(() => {
        const chart = this.chartComponent();
        if (chart) {
          chart.loadCachedData({
            bars: session.chartSnapshot!.bars,
            indicators: session.chartSnapshot!.indicators,
            quality: session.chartSnapshot!.quality,
            allowedTimeframes: session.chartSnapshot!.allowedTimeframes,
            estimatedBarsPerTimeframe: session.chartSnapshot!.estimatedBarsPerTimeframe,
            recommendedTimeframe: session.chartSnapshot!.recommendedTimeframe,
            visibleIndicatorIds: session.chartSnapshot!.visibleIndicatorIds,
            timeframe: session.chartSnapshot!.timeframe,
          });
        }
      });
    }

    this.sessionPanelOpen.set(false);
  }

  async deleteSession(id: string, event: Event): Promise<void> {
    event.stopPropagation();
    await this.sessionService.deleteSession(id);
    if (this.activeSessionId() === id) {
      this.activeSessionId.set(null);
      this.sessionName.set('');
    }
    await this.refreshSessionList();
  }

  startRenaming(session: DataLabSessionSummary, event: Event): void {
    event.stopPropagation();
    this.renamingSessionId.set(session.id);
    this.renameValue.set(session.name);
  }

  async confirmRename(id: string): Promise<void> {
    if (this.renameValue().trim()) {
      await this.sessionService.renameSession(id, this.renameValue().trim());
      await this.refreshSessionList();
    }
    this.renamingSessionId.set(null);
  }

  cancelRename(): void {
    this.renamingSessionId.set(null);
  }

  formatDate(timestamp: string): string {
    return new Date(timestamp).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  detachSession(): void {
    this.activeSessionId.set(null);
    this.sessionName.set('');
  }

  // ── Holiday & date preset support ─────────────────────────

  private loadHolidays(): void {
    firstValueFrom(this.marketMonitor.getHolidays(20))
      .then(events => this.holidays.set(events))
      .catch(() => {}); // non-critical — calendar still works without holidays
  }

  /** Look up whether a calendar cell date is a holiday. Month is 0-indexed from PrimeNG. */
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
    const to = DataLabComponent.getYesterday();
    const from = new Date(to);
    from.setDate(from.getDate() - daysBack);
    this.fromDateValue.set(from);
    this.toDateValue.set(to);
  }

  setPresetMonths(months: number): void {
    const to = DataLabComponent.getYesterday();
    const from = new Date(to);
    from.setMonth(from.getMonth() - months);
    this.fromDateValue.set(from);
    this.toDateValue.set(to);
  }

  async loadAvailableIndicators(): Promise<void> {
    this.loadingIndicators.set(true);
    try {
      const response = await firstValueFrom(
        this.http.get<AvailableResponse>(
          `${environment.pythonServiceUrl}/api/dataset/available`
        )
      );

      if (!response.success) {
        this.error.set('Failed to load indicators');
        return;
      }

      const catList: CategoryData[] = [];
      const map: Record<string, IndicatorInfo> = {};
      for (const [catName, items] of Object.entries(response.categories)) {
        catList.push({ name: catName, indicators: items });
        for (const item of items) {
          map[item.name] = item;
        }
      }
      this.categories.set(catList);
      this.indicatorMap.set(map);
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loadingIndicators.set(false);
    }
  }

  toggleCategory(catName: string): void {
    this.expandedCategories.update(set => {
      const next = new Set(set);
      if (next.has(catName)) {
        next.delete(catName);
      } else {
        next.add(catName);
      }
      return next;
    });
  }

  isCategoryExpanded(catName: string): boolean {
    return this.expandedCategories().has(catName);
  }

  isSelected(name: string): boolean {
    return this.selectedNames().has(name);
  }

  categorySelectedCount(catName: string): number {
    const cat = this.categories().find(c => c.name === catName);
    if (!cat) return 0;
    const names = this.selectedNames();
    return cat.indicators.filter(i => names.has(i.name)).length;
  }

  toggleIndicator(ind: IndicatorInfo): void {
    if (this.isSelected(ind.name)) {
      // Remove all entries for this indicator
      this.entries.update(list => list.filter(e => e.name !== ind.name));
    } else {
      // Add with defaults
      const defaults: Record<string, number> = {};
      for (const p of ind.configurable_params) {
        defaults[p.name] = p.default;
      }
      this.entries.update(list => [...list, { name: ind.name, params: defaults }]);
    }
  }

  addInstance(ind: IndicatorInfo): void {
    const defaults: Record<string, number> = {};
    for (const p of ind.configurable_params) {
      defaults[p.name] = p.default;
    }
    this.entries.update(list => [...list, { name: ind.name, params: defaults }]);
  }

  removeEntry(index: number): void {
    this.entries.update(list => list.filter((_, i) => i !== index));
  }

  updateEntryParam(index: number, paramName: string, value: number): void {
    this.entries.update(list => {
      const next = [...list];
      next[index] = { ...next[index], params: { ...next[index].params, [paramName]: value } };
      return next;
    });
  }

  clearAll(): void {
    this.entries.set([]);
  }

  resetDefaults(): void {
    this.entries.set([...DEFAULT_ENTRIES]);
  }

  entryLabel(entry: IndicatorEntry): string {
    const parts = Object.entries(entry.params).map(([, v]) => v);
    return parts.length ? `${entry.name}(${parts.join(', ')})` : entry.name;
  }

  getConfigParams(name: string): ParamConfig[] {
    return this.indicatorMap()[name]?.configurable_params ?? [];
  }

  getTooltipText(ind: IndicatorInfo): string {
    return INDICATOR_META[ind.name]?.tooltipSummary ?? ind.description;
  }

  resetEntryToDefaults(index: number): void {
    const entry = this.entries()[index];
    const info = this.indicatorMap()[entry.name];
    if (!info) return;
    const defaults: Record<string, number> = {};
    for (const p of info.configurable_params) {
      defaults[p.name] = p.default;
    }
    this.entries.update(list => {
      const next = [...list];
      next[index] = { ...next[index], params: defaults };
      return next;
    });
  }

  async generateZip(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    this.progress.set('Fetching OHLCV data and calculating indicators...');

    try {
      const optionsConfig = this.optionsCompanionEnabled()
        ? {
            enabled: true,
            strikes_each_side: this.optionsStrikesEachSide(),
            include_calls: this.optionsIncludeCalls(),
            include_puts: this.optionsIncludePuts(),
            expiry_mode: this.optionsExpiryMode(),
            max_dte: this.optionsMaxDte(),
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
            risk_free_rate: this.optRiskFreeRate(),
            dividend_yield: this.optDividendYield(),
          }
        : null;

      const payload = {
        ticker: this.ticker(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        indicator_entries: this.entries(),
        session: this.session(),
        forward_fill: this.forwardFill(),
        adjusted: this.adjustForSplits(),
        adjust_for_dividends: this.adjustForDividends(),
        warmup: this.warmup(),
        timespan: this.timespan(),
        multiplier: this.multiplier(),
        sort: this.sort(),
        limit: this.polygonLimit(),
        options_companion: optionsConfig,
        include_quality_report: this.includeQualityReportInZip(),
        include_splits: this.includeSplits(),
        include_dividends: this.includeDividends(),
        include_ticker_overview: this.includeTickerOverview(),
        include_news: this.includeNews(),
        include_financials: this.includeFinancials(),
        include_trades: this.includeStockTrades(),
        include_quotes: this.includeStockQuotes(),
      };

      const blob = await firstValueFrom(
        this.http.post(
          `${environment.pythonServiceUrl}/api/dataset/generate-zip`,
          payload,
          { responseType: 'blob' }
        )
      );

      const sessionLabel = this.session() === 'rth' ? 'rth' : 'ext';
      const tsLabel = this.multiplier() > 1 ? `${this.multiplier()}${this.timespan()}` : this.timespan();
      this.progress.set('Downloading ZIP...');
      this.downloadBlob(blob, `${this.ticker()}_${tsLabel}_${sessionLabel}_${this.fromDate()}_to_${this.toDate()}.zip`);
      this.progress.set('Done! ZIP downloaded (dataset.csv + metadata.csv + columns.csv).');
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
      this.progress.set('');
    } finally {
      this.loading.set(false);
    }
  }

  onOurCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.ourCsvFile.set(input.files?.[0] ?? null);
  }

  onTvCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.tvCsvFile.set(input.files?.[0] ?? null);
  }

  async runValidation(): Promise<void> {
    const ourFile = this.ourCsvFile();
    const tvFile = this.tvCsvFile();
    if (!ourFile || !tvFile) return;

    this.loadingValidation.set(true);
    this.error.set('');
    this.validationReport.set('');

    try {
      const formData = new FormData();
      formData.append('our_csv', ourFile);
      formData.append('tv_csv', tvFile);
      formData.append('ticker', this.ticker());

      const response = await firstValueFrom(
        this.http.post<{ success: boolean; report: string }>(
          `${environment.pythonServiceUrl}/api/dataset/validation-report`,
          formData
        )
      );

      if (response.success) {
        this.validationReport.set(response.report);
      } else {
        this.error.set('Validation report generation failed');
      }
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loadingValidation.set(false);
    }
  }

  downloadValidationReport(): void {
    const report = this.validationReport();
    if (!report) return;
    const blob = new Blob([report], { type: 'text/markdown' });
    this.downloadBlob(blob, `${this.ticker()}_validation_report.md`);
  }

  // ── Data Quality Report panel ─────────────────────────────

  async runQualityReport(): Promise<void> {
    this.qualityReportLoading.set(true);
    this.qualityReportError.set('');
    this.qualityReportResult.set(null);
    try {
      const res = await firstValueFrom(
        this.http.post<QualityReportResponse>(
          `${environment.pythonServiceUrl}/api/data-quality/analyze`,
          {
            ticker: this.ticker(),
            from_date: this.fromDate(),
            to_date: this.toDate(),
            volume_fix: 'round',
            recompute_indicators: false,
            indicator_entries: [],
          }
        )
      );
      this.qualityReportResult.set(res);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      this.qualityReportError.set(msg);
    } finally {
      this.qualityReportLoading.set(false);
    }
  }

  downloadQualityReportMd(): void {
    const report = this.qualityReportResult();
    if (!report) return;
    const md = this.buildQualityReportMarkdown(report);
    const blob = new Blob([md], { type: 'text/markdown' });
    this.downloadBlob(blob, `${report.ticker}_quality_report.md`);
  }

  private buildQualityReportMarkdown(r: QualityReportResponse): string {
    const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const lines: string[] = [];
    lines.push(`# Data Quality Report — ${r.ticker}`);
    lines.push('');
    lines.push(`**Range:** ${r.from_date} → ${r.to_date}  **Generated:** ${now}`);
    lines.push('');
    lines.push('## Summary');
    lines.push('');
    lines.push('| Metric | Raw | Clean | Δ |');
    lines.push('|---|---:|---:|---:|');
    const metrics: [string, keyof QualityReportSummary][] = [
      ['Total bars', 'total_bars'],
      ['Trading days', 'trading_days'],
      ['Zero-volume bars', 'zero_volume_bars'],
      ['Flat bars (O=H=L=C)', 'flat_bars_ohlc_equal'],
      ['Fractional volume bars', 'fractional_volume_bars'],
      ['VWAP > high violations', 'vwap_above_high'],
      ['VWAP < low violations', 'vwap_below_low'],
      ['OHLC violations', 'ohlc_violations'],
      ['Duplicate timestamps', 'duplicate_timestamps'],
      ['Weekend bars', 'weekend_bars'],
      ['Intraday gaps', 'intraday_gaps'],
    ];
    for (const [label, key] of metrics) {
      const rawVal = r.raw_summary[key];
      const cleanVal = r.clean_summary[key];
      const delta = cleanVal - rawVal;
      const deltaStr = delta > 0 ? `+${delta}` : `${delta}`;
      lines.push(`| ${label} | ${rawVal} | ${cleanVal} | ${deltaStr} |`);
    }
    lines.push('');
    lines.push('## Pipeline steps');
    lines.push('');
    for (const step of r.steps) {
      lines.push(`### ${step.order}. ${step.name}`);
      lines.push(`*Library:* \`${step.library}\``);
      lines.push('');
      lines.push(step.description);
      lines.push('');
      lines.push(`Bars: ${step.bars_before} → ${step.bars_after} (${step.bars_removed} removed)`);
      lines.push('');
    }
    return lines.join('\n');
  }

  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}
