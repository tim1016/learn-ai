/* eslint-disable @typescript-eslint/no-explicit-any, @typescript-eslint/no-non-null-assertion, unused-imports/no-unused-vars */
import {
  Component, signal, computed, effect, inject, untracked, viewChild, ElementRef,
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
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { ActiveIndicatorCardComponent } from './active-indicator-card/active-indicator-card.component';
import { ActiveIndicatorGroupComponent, IndicatorGroupItem } from './active-indicator-group/active-indicator-group.component';
import { PastChainInspectorComponent } from './past-chain-inspector/past-chain-inspector.component';
import { IndicatorConfigModalComponent } from './indicator-config-modal/indicator-config-modal.component';
import { RunDockComponent } from '../../shared/run-dock/run-dock.component';
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
} from '../../shared/run-dock/run-dock-source';
import { INDICATOR_REFERENCE } from '../../shared/indicators/indicator-reference';
import {
  IndicatorPickerAdd,
  IndicatorPickerComponent,
} from '../../shared/indicator-picker/indicator-picker.component';

type EntriesSortMode = 'insertion' | 'category' | 'name';

interface EntriesViewSingle {
  kind: 'single';
  item: { entry: IndicatorEntry; originalIndex: number };
}
interface EntriesViewGroup {
  kind: 'group';
  name: string;
  items: IndicatorGroupItem[];
}
type EntriesViewBlock = EntriesViewSingle | EntriesViewGroup;
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
  parseYmd,
  formatYmd,
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

/** Count weekdays (Mon–Fri) in [from, to] inclusive. Holidays ignored — the
 *  ~5 % overcount is acceptable for the rough options-contract estimate. */
function countWeekdays(from: string, to: string): number {
  if (!from || !to) return 0;
  const start = new Date(`${from}T00:00:00Z`);
  const end = new Date(`${to}T00:00:00Z`);
  if (isNaN(start.getTime()) || isNaN(end.getTime()) || end < start) return 0;
  const oneDay = 86_400_000;
  const days = Math.floor((end.getTime() - start.getTime()) / oneDay) + 1;
  let weekdays = 0;
  for (let i = 0; i < days; i++) {
    const dow = new Date(start.getTime() + i * oneDay).getUTCDay();
    if (dow !== 0 && dow !== 6) weekdays++;
  }
  return weekdays;
}

/** Approximate RTH bars per trading day for a given timespan/multiplier. */
function barsPerTradingDay(
  timespan: 'second' | 'minute' | 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year',
  multiplier: number,
): number {
  const m = Math.max(1, multiplier);
  switch (timespan) {
    case 'second': return Math.ceil(23_400 / m);     // 6.5h × 60 × 60
    case 'minute': return Math.ceil(390 / m);        // 6.5h × 60
    case 'hour':   return Math.ceil(7 / m);          // 6.5h rounded up
    case 'day':
    case 'week':
    case 'month':
    case 'quarter':
    case 'year':   return 1;
  }
}

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

export type PolygonTimespan = 'minute' | 'hour' | 'day' | 'week' | 'month';

/**
 * Parse the chart component's timeframe vocabulary ("1m", "5m", "15m",
 * "1h", "4h", "1D", "1W", "1M") into Polygon's (timespan, multiplier)
 * pair. Returns null for unrecognized inputs so callers can ignore
 * vocabulary the dataset endpoint can't honor.
 */
export function parseChartTimeframe(
  timeframe: string,
): { timespan: PolygonTimespan; multiplier: number } | null {
  const match = timeframe.match(/^(\d+)([mhDWM])$/);
  if (!match) return null;
  const multiplier = parseInt(match[1], 10);
  const unit = match[2];
  const timespan: PolygonTimespan | null =
    unit === 'm' ? 'minute' :
    unit === 'h' ? 'hour' :
    unit === 'D' ? 'day' :
    unit === 'W' ? 'week' :
    unit === 'M' ? 'month' :
    null;
  if (!timespan) return null;
  return { timespan, multiplier };
}

/**
 * Layman-friendly readout for the Auto Chunk control. Pure helper so the
 * exact wording can be regression-tested without spinning up the
 * component (the actual ``autoChunkReadout`` computed signal forwards
 * to this function). Wording intentionally stays plan-tier-agnostic —
 * the server-side throttle decides whether to pace, and quoting a
 * specific per-minute number here would be wrong on paid plans.
 */
export function formatChunkReadout(
  bars: number,
  autoChunk: boolean,
  polygonLimit: number,
): string {
  const limit = Math.max(1, polygonLimit);
  const chunks = Math.max(1, Math.ceil(bars / limit));
  if (!autoChunk) {
    return `Manual: ${polygonLimit.toLocaleString()} bars per request.`;
  }
  if (chunks === 1) {
    return `1 request · ~${bars.toLocaleString()} bars · single response.`;
  }
  return `Plan runs ${chunks} requests · ~${bars.toLocaleString()} bars · paced if your plan caps requests/min.`;
}

interface ParamConfig {
  name: string;
  /** Mirrors PythonDataService INDICATOR_CONFIGS — only 'int' or 'float'. */
  type: 'int' | 'float';
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
  imports: [
    CommonModule, FormsModule, RouterModule,
    DataLabChartComponent, SharedModule, Tooltip,
    PageHeaderComponent, TickerRangePickerComponent,
    ActiveIndicatorCardComponent, ActiveIndicatorGroupComponent, IndicatorConfigModalComponent,
    PastChainInspectorComponent, RunDockComponent, IndicatorPickerComponent,
  ],
  templateUrl: './data-lab.component.html',
  styleUrls: ['./data-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [
    // Wire data-lab's RunSessionService into the shared dock's source slot.
    // Unique storage key so data-lab and engine-lab don't share dock state.
    { provide: RUN_DOCK_SOURCE, useExisting: RunSessionService },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: 'run-dock-expanded:data-lab' },
  ],
})
export class DataLabComponent {
  private http = inject(HttpClient);
  private sessionService = inject(DataLabSessionService);
  private marketMonitor = inject(MarketMonitorService);
  /** Drives the streaming Fetch & bundle progress surface (states B/C/D/E). */
  readonly runSession = inject(RunSessionService);

  /** Reference to the chart child so we can call loadCachedData(). */
  chartComponent = viewChild<DataLabChartComponent>('chartComponent');

  readonly catalogSearchInput = viewChild<ElementRef<HTMLInputElement>>('catalogSearchInput');

  /** Whether the chart has been requested at least once. The chart is
   *  hidden via @if until the user clicks Fetch — initial page load
   *  shows no chart-shaped placeholder. After the first fetch, the chart
   *  stays mounted so subsequent fetches can flow through the viewchild
   *  without re-mount timing issues. */
  chartRendered = signal(false);

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
  fromDateValue = signal<Date>(DataLabComponent.get30DaysAgo());
  toDateValue = signal<Date>(DataLabComponent.getYesterday());
  fromDate = computed(() => formatYmd(this.fromDateValue()));
  toDate = computed(() => formatYmd(this.toDateValue()));

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
  includeVwapColumn = signal(true);
  includeTransactionsColumn = signal(true);
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
    from: formatYmd(DataLabComponent.get30DaysAgo()),
    to: formatYmd(DataLabComponent.getYesterday()),
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

  /**
   * The bar timeframe in the data-lab-chart vocabulary
   * ("1m"/"5m"/"15m"/"1h"/"4h"/"1D"/"1W"/"1M"). The chart no longer owns
   * a selector; it consumes this signal as its `timeframe` input. Maps
   * the parent's (timespan, multiplier) pair to the chart-endpoint
   * vocabulary — note the day case uses "1D" (uppercase) which is what
   * the chart endpoint expects.
   */
  readonly chartTimeframe = computed<string>(() => {
    const t = this.timespan();
    const m = this.multiplier();
    if (t === 'minute') return `${m}m`;
    if (t === 'hour') return `${m}h`;
    if (t === 'day') return m === 1 ? '1D' : `${m}D`;
    if (t === 'week') return `${m}W`;
    if (t === 'month') return `${m}M`;
    return '1D';
  });

  /**
   * Apply the chart endpoint's recommendation when it rejects a fetch
   * with TIMEFRAME_NOT_ALLOWED. The chart endpoint caps individual
   * fetches at ~20k bars; the parent's bar-count safety net only fires
   * above 250k expected bars, so without this handler manual picks in
   * the 20k–250k range would fail silently with no correction.
   *
   * Switches off Auto so the user's intent is preserved on the next
   * range change (Auto would otherwise re-pick from the date span and
   * potentially overshoot again).
   */
  onChartTimeframeRejected(event: { requested: string; recommended: string; detail: string }): void {
    const parsed = parseChartTimeframe(event.recommended);
    if (!parsed) return;
    this.autoBarTimeframe.set(false);
    this.timespan.set(parsed.timespan);
    this.multiplier.set(parsed.multiplier);
    // Re-fetch with the new timeframe so the user sees the chart they
    // expected. Defer to setTimeout so Angular has time to propagate the
    // updated `timeframe` input through to the chart child.
    setTimeout(() => {
      const chart = this.chartComponent();
      if (chart) chart.fetchData();
    });
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

  /** Threshold above which the bar-count safety-net effect forces a
   *  fall-back to the auto-resolution heuristic. 250k is the same ceiling
   *  the picker's "wide minute range" advisory uses, so the two surfaces
   *  agree on what counts as "too many bars". */
  private static readonly MAX_SAFE_BAR_COUNT = 250_000;

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
   * Polygon requests the fetch will issue. Paid plans run back-to-back
   * (no per-minute cap); only the free Basic tier triggers the
   * server-side throttle that paces requests to 5/min. The actual
   * pacing decision is made server-side by the throttle config; this
   * readout intentionally avoids quoting a specific per-minute number
   * so it stays accurate for both tiers.
   */
  readonly autoChunkReadout = computed<string>(() =>
    formatChunkReadout(this.expectedBarCount(), this.autoChunk(), this.polygonLimit()),
  );

  loading = signal(false);
  loadingIndicators = signal(false);
  loadingValidation = signal(false);
  error = signal('');
  progress = signal('');

  // ── Options companion config ───────────────────────────────
  optionsCompanionEnabled = signal(false);
  optionsStrikesEachSide = signal(3);
  optionsIncludeCalls = signal(true);
  optionsIncludePuts = signal(true);
  // Strict DTE distance: 0 = 0DTE (same-day expiry); >0 = constant
  // calendar-day distance from each trading day to its target expiry.
  // Replaces the legacy expiry_mode + max_dte pair.
  optionsDteDistance = signal(0);
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
  optIncludeDiscontinuity = signal(true);
  optRiskFreeRate = signal(0.05);
  optDividendYield = signal(0.0);

  // Tickers with verified daily (Mon-Fri) expiries as of 2026
  readonly DAILY_EXPIRY_TICKERS = ['SPY','QQQ','IWM','SPX','XSP','NDX','XND','DIA','VIX'];
  tickerSupportsDaily = computed(() =>
    this.DAILY_EXPIRY_TICKERS.includes(this.ticker().toUpperCase())
  );

  /**
   * Estimated number of option contracts that will be pulled given the
   * current date range, DTE distance, strike count, and calls/puts toggles.
   *
   *   matchingDays ≈ trading_days_in_range          (daily-expiry tickers)
   *                ≈ ceil(trading_days_in_range/5)  (weekly-only tickers)
   *   contracts    = matchingDays × (2·strikesEachSide + 1) × (#sides)
   *   bars         = contracts × barsPerTradingDay
   *
   * Under the strict-DTE policy, each trading day picks one fresh chain
   * of contracts and we fetch only that day's bars from each — the
   * `daysPerContract` factor from the legacy `nearest_within_days` mode
   * is gone. Trading days are approximated as weekdays in [from, to];
   * the holiday calendar is ignored at this resolution.
   */
  optionsContractEstimate = computed<{ contracts: number; expiries: number; bars: number }>(() => {
    if (!this.optionsCompanionEnabled()) return { contracts: 0, expiries: 0, bars: 0 };
    const sides = (this.optionsIncludeCalls() ? 1 : 0) + (this.optionsIncludePuts() ? 1 : 0);
    if (sides === 0) return { contracts: 0, expiries: 0, bars: 0 };
    const tradingDays = countWeekdays(this.fromDate(), this.toDate());
    if (tradingDays <= 0) return { contracts: 0, expiries: 0, bars: 0 };
    const isDaily = this.tickerSupportsDaily();
    const matchingDays = isDaily ? tradingDays : Math.max(1, Math.ceil(tradingDays / 5));
    const strikes = this.optionsStrikesEachSide() * 2 + 1;
    const contracts = matchingDays * strikes * sides;
    const barsPerDay = barsPerTradingDay(this.timespan(), this.multiplier());
    const bars = contracts * barsPerDay;
    return { contracts, expiries: matchingDays, bars };
  });

  /**
   * Severity classification for the contract-estimate badge.
   *
   * Daily-expiry tickers (SPY, QQQ, IWM, …) accumulate contracts much
   * faster than weekly-only ones — the same window of trading days
   * produces ~5× the contract count when every weekday is also an
   * expiry. Thresholds are split accordingly.
   */
  optionsContractEstimateSeverity = computed<'ok' | 'warn' | 'danger'>(() => {
    const c = this.optionsContractEstimate().contracts;
    const daily = this.tickerSupportsDaily();
    const warn = daily ? 2_000 : 5_000;
    const danger = daily ? 8_000 : 20_000;
    if (c >= danger) return 'danger';
    if (c >= warn) return 'warn';
    return 'ok';
  });

  optionsResolutionWarning = computed(() => {
    if (!this.optionsCompanionEnabled()) return '';
    const ts = this.timespan();
    const mult = this.multiplier();
    if (ts === 'day') {
      return 'At day resolution, each contract produces exactly 1 row per trading day. You probably want minute or hour resolution for intraday IV/Greeks.';
    }
    if (ts === 'hour') {
      const rowsPerDay = Math.max(1, Math.floor(7 / Math.max(mult, 1)));
      return `At ${mult}-hour resolution, each contract produces ~${rowsPerDay} rows per trading day — limited intraday granularity.`;
    }
    return '';
  });

  // ── Data Quality Report panel state ────────────────────────
  includeQualityReportInZip = signal(false);
  qualityReportLoading = signal(false);
  qualityReportResult = signal<QualityReportResponse | null>(null);
  qualityReportError = signal('');

  // ── Dataset column toggles ────────────────────────────────
  includePreviousClose = signal(true);

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

  /** Search query for the indicator catalog — filters by name or description. */
  catalogQuery = signal('');

  // Volume warning — set after chart data loads
  volumeWarning = signal('');

  // Selected indicator names (unique set for checkbox state).
  // Was a getter that allocated a new Set on every template read — the
  // category grid called it ~80 times per render, and each keystroke in
  // a param input triggered ~1,040 Set allocations (audit § 4.3).
  // As a computed it memoizes until entries() changes.
  selectedNames = computed<Set<string>>(() => new Set(this.entries().map(e => e.name)));

  filteredCategories = computed<CategoryData[]>(() => {
    const q = this.catalogQuery().trim().toLowerCase();
    if (!q) return this.categories();
    return this.categories()
      .map(c => ({
        ...c,
        indicators: c.indicators.filter(
          i => i.name.toLowerCase().includes(q) || i.description.toLowerCase().includes(q),
        ),
      }))
      .filter(c => c.indicators.length > 0);
  });

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
    const base = ['unix_ts', 'iso_time', 'open', 'high', 'low', 'close', 'volume'];
    if (this.includeVwapColumn()) base.push('vwap');
    if (this.includeTransactionsColumn()) base.push('transactions');
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

  private static readonly BASE_COL_SET = new Set(['unix_ts', 'iso_time']);
  private static readonly OHLCV_COL_SET = new Set([
    'open', 'high', 'low', 'close', 'volume', 'vwap', 'transactions',
  ]);

  /** Columns split into Base / OHLCV / Indicators for the color-coded preview. */
  columnGroups = computed(() => {
    const base: string[] = [];
    const ohlcv: string[] = [];
    const indicators: string[] = [];
    for (const col of this.estimatedColumns()) {
      if (DataLabComponent.BASE_COL_SET.has(col)) base.push(col);
      else if (DataLabComponent.OHLCV_COL_SET.has(col)) ohlcv.push(col);
      else indicators.push(col);
    }
    return { base, ohlcv, indicators };
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
        // Only the picker's `rangeState` changes should trigger this
        // effect's writes — every other signal read goes through
        // `untracked()` so it isn't registered as a dep. Without this, a
        // user picking 15m via the bar-timeframe dropdown would race the
        // sibling timespan→resolution effect: whichever ran first saw
        // stale state and the late-arriving Effect A here clobbered the
        // multiplier back to 1, silently undoing the manual pick.
        untracked(() => {
          if (this.ticker() !== v.symbol) this.ticker.set(v.symbol);
          const fromIso = formatYmd(this.fromDateValue());
          const toIso = formatYmd(this.toDateValue());
          if (fromIso !== v.from) {
            const parsed = parseYmd(v.from);
            if (parsed) this.fromDateValue.set(parsed);
          }
          if (toIso !== v.to) {
            const parsed = parseYmd(v.to);
            if (parsed) this.toDateValue.set(parsed);
          }
          const expected = DataLabComponent.timespanToResolution(this.timespan());
          if (v.resolution !== expected) {
            this.timespan.set(v.resolution === 'daily' ? 'day' : v.resolution);
            // Reset multiplier so the bar-timeframe dropdown matches a
            // preset after a coarse resolution change via the picker.
            this.multiplier.set(1);
          }
        });
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

    // Push the bar-timeframe back into the picker's `resolution` field so
    // the picker's range-vs-resolution advisories reflect the user's actual
    // pick. Without this, the picker keeps its own copy and would warn
    // about "minute × N days" even when the user had switched to hour.
    // The complementary picker → timespan flow lives in the rangeState
    // effect above; this is the reverse direction. Only timespan-driven
    // re-runs are intended here — read rangeState through `untracked()`
    // so this effect doesn't loop with the picker→timespan one.
    effect(
      () => {
        const expectedResolution = DataLabComponent.timespanToResolution(this.timespan());
        untracked(() => {
          const current = this.rangeState();
          if (current.resolution === expectedResolution) return;
          this.rangeState.set({ ...current, resolution: expectedResolution });
        });
      },
      { allowSignalWrites: true },
    );

    // Bar-count safety net: if the (range × timeframe × session) combo
    // would overshoot a sane number of bars, fall back to the auto
    // heuristic. This kicks in when a manual pick + a wide range would
    // generate hundreds of thousands of rows. The auto toggle is forced
    // on so the user can see why their pick was overridden via the
    // existing "Auto picked X" hint.
    effect(
      () => {
        const expected = this.expectedBarCount();
        if (expected <= DataLabComponent.MAX_SAFE_BAR_COUNT) return;
        const picked = pickAutoBarTimeframe(this.spanCalendarDays());
        if (this.activeBarTimeframe() === picked) return;
        this.autoBarTimeframe.set(true);
        this.setBarTimeframe(picked);
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
    if (!this.chartRendered()) {
      // First click: mount the chart, then defer the fetch to a setTimeout
      // so Angular has time to process the @if and populate the viewchild.
      this.chartRendered.set(true);
      setTimeout(() => {
        const chart = this.chartComponent();
        if (chart) chart.fetchData();
      });
    } else {
      // Chart is already mounted — fetch via the viewchild directly.
      const chart = this.chartComponent();
      if (chart) chart.fetchData();
    }
    if (this.alsoGenerateZip()) {
      await this.runSession.start(this._buildGenerateZipPayload());
    }
  }

  /** Hand-off to the streaming pipeline — the same payload the legacy
   *  ``generateZip`` endpoint expects. */
  private _buildGenerateZipPayload(): Record<string, unknown> {
    const optionsConfig = this.optionsCompanionEnabled()
      ? {
          enabled: true,
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
      include_previous_close: this.includePreviousClose(),
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
    const restoredFrom = parseYmd(session.config.fromDate);
    if (restoredFrom) this.fromDateValue.set(restoredFrom);
    const restoredTo = parseYmd(session.config.toDate);
    if (restoredTo) this.toDateValue.set(restoredTo);
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

      // Mirror the snapshot's timeframe into parent state — the chart no
      // longer owns this signal, so the parent must set it before the
      // chart mounts and reads its `timeframe` input.
      const parsed = parseChartTimeframe(session.chartSnapshot.timeframe);
      if (parsed) {
        this.autoBarTimeframe.set(false);
        this.timespan.set(parsed.timespan);
        this.multiplier.set(parsed.multiplier);
      }

      // Mount the chart (it's hidden until chartRendered flips on) before
      // pushing data into it.
      this.chartRendered.set(true);

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

  focusCatalogSearch(): void {
    // Old inline catalog had a search input; the new picker doesn't.
    // Scroll the picker into view as the closest replacement so users who
    // click "Add indicator" from the active list still get nudged to it.
    document.getElementById('data-lab-indicator-catalog')?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    });
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

  // ── Active-indicator modal state ──────────────────────────
  /** Index of the entry currently being configured in the modal, or null. */
  configuringIndex = signal<number | null>(null);
  configureModalVisible = computed(() => this.configuringIndex() !== null);
  configuringEntry = computed(() => {
    const i = this.configuringIndex();
    return i === null ? null : (this.entries()[i] ?? null);
  });
  configuringParams = computed<ParamConfig[]>(() => {
    const e = this.configuringEntry();
    return e ? this.getConfigParams(e.name) : [];
  });

  openConfigure(index: number): void {
    this.configuringIndex.set(index);
  }

  onConfigureModalVisibleChange(open: boolean): void {
    if (!open) this.configuringIndex.set(null);
  }

  onModalParamChange(change: { name: string; value: number }): void {
    const i = this.configuringIndex();
    if (i === null) return;
    this.updateEntryParam(i, change.name, change.value);
  }

  onModalReset(): void {
    const i = this.configuringIndex();
    if (i === null) return;
    this.resetEntryToDefaults(i);
  }

  onModalResetParam(paramName: string): void {
    const i = this.configuringIndex();
    if (i === null) return;
    const entry = this.entries()[i];
    const info = this.indicatorMap()[entry.name];
    const param = info?.configurable_params.find((p) => p.name === paramName);
    if (!param) return;
    this.updateEntryParam(i, paramName, param.default);
  }

  /** Add an indicator (by key) as a new instance, used by modal "related" chips. */
  addInstanceByName(name: string): void {
    const info = this.indicatorMap()[name];
    if (!info) return;
    this.addInstance(info);
  }

  // ── New indicator-picker bridge ───────────────────────────────
  /** Names of indicators with at least one active instance — drives the
   *  picker's per-row +N badge. Repeats reflect instance count. The other
   *  `activeIndicatorKeys` computed (deduped) further down feeds the modal,
   *  which has different semantics. */
  pickerActiveKeys = computed<readonly string[]>(() =>
    this.entries().map(e => e.name),
  );

  /** Handler for the picker's (add) + (addInstance) outputs. Both events
   *  append a new entry; "add" semantically means first-time, "addInstance"
   *  means another instance of an already-staged indicator — but downstream
   *  the storage shape is identical (an IndicatorEntry per instance). */
  onPickerAdd(event: IndicatorPickerAdd): void {
    const info = this.indicatorMap()[event.name];
    const params: Record<string, number> = info
      ? this.fillMissingParamDefaults(event.params, info)
      : { ...event.params };
    this.entries.update(list => [...list, { name: event.name, params }]);
  }

  private fillMissingParamDefaults(
    given: Record<string, number>,
    info: IndicatorInfo,
  ): Record<string, number> {
    const out: Record<string, number> = { ...given };
    for (const p of info.configurable_params) {
      if (!(p.name in out)) out[p.name] = p.default;
    }
    return out;
  }

  // ── Preview-mode state (catalog → modal) ──────────────────
  /** When non-null, the modal opens in preview mode for the given key. */
  previewKey = signal<string | null>(null);
  modalMode = computed<'configure' | 'preview'>(() =>
    this.previewKey() !== null ? 'preview' : 'configure',
  );
  previewEntry = computed<IndicatorEntry | null>(() => {
    const key = this.previewKey();
    if (!key) return null;
    const defaults: Record<string, number> = {};
    const info = this.indicatorMap()[key];
    if (info) for (const p of info.configurable_params) defaults[p.name] = p.default;
    return { name: key, params: defaults };
  });

  /** Modal entry to render — either the configured active entry or the
   *  preview synthetic entry. */
  modalEntry = computed<IndicatorEntry | null>(() => {
    return this.previewEntry() ?? this.configuringEntry();
  });

  /** Modal param configs for whichever entry is open (preview or active). */
  modalParamConfigs = computed<ParamConfig[]>(() => {
    const e = this.modalEntry();
    return e ? this.getConfigParams(e.name) : [];
  });

  /** Modal visibility — open whenever we have either a preview or a
   *  configure index. */
  modalVisible = computed(() => this.previewKey() !== null || this.configuringIndex() !== null);

  openPreview(key: string): void {
    this.previewKey.set(key);
  }

  onModalVisibleChange(open: boolean): void {
    if (!open) {
      this.previewKey.set(null);
      this.configuringIndex.set(null);
    }
  }

  /** Preview "Add to active with these params" — adds a new instance. */
  onAddPreview(payload: { key: string; params: Record<string, number> }): void {
    const info = this.indicatorMap()[payload.key];
    if (!info) return;
    this.entries.update((list) => [...list, { name: payload.key, params: { ...payload.params } }]);
  }

  /** Remove the *last* active entry whose key matches — used by modal undo
   *  and the "Remove" state of related-indicator chips. */
  removeLastEntryByName(name: string): void {
    const list = this.entries();
    for (let i = list.length - 1; i >= 0; i--) {
      if (list[i].name === name) {
        this.removeEntry(i);
        return;
      }
    }
  }

  /** Active indicator keys (deduped) — supplied to the modal so related
   *  chips can render their tri-state (idle / pending-undo / already-active). */
  activeIndicatorKeys = computed<readonly string[]>(() =>
    Array.from(new Set(this.entries().map((e) => e.name)))
  );

  // ── Active-indicators sort + add affordances ──────────────
  readonly entriesSortModes: { value: EntriesSortMode; label: string }[] = [
    { value: 'insertion', label: 'Order added' },
    { value: 'category',  label: 'Category' },
    { value: 'name',      label: 'Name (A→Z)' },
  ];
  entriesSortMode = signal<EntriesSortMode>('insertion');

  /** Display-only view of entries with their original indices preserved so
   *  the template can still call removeEntry/openConfigure with the right
   *  index regardless of sort. */
  sortedEntriesView = computed<{ entry: IndicatorEntry; originalIndex: number }[]>(() => {
    const list = this.entries().map((entry, originalIndex) => ({ entry, originalIndex }));
    const mode = this.entriesSortMode();
    if (mode === 'insertion') return list;
    if (mode === 'name') {
      return list.sort((a, b) => a.entry.name.localeCompare(b.entry.name));
    }
    // category: stable-sort by category order, then by name
    const order: Record<string, number> = { trend: 0, momentum: 1, volatility: 2, volume: 3 };
    return list.sort((a, b) => {
      const ca = INDICATOR_REFERENCE[a.entry.name]?.category ?? 'trend';
      const cb = INDICATOR_REFERENCE[b.entry.name]?.category ?? 'trend';
      const diff = (order[ca] ?? 99) - (order[cb] ?? 99);
      return diff !== 0 ? diff : a.entry.name.localeCompare(b.entry.name);
    });
  });

  /**
   * Group consecutive runs of the same indicator key (≥ 4) into a single
   * `kind: 'group'` entry; everything else passes through as `kind: 'single'`.
   * Threshold matches the design brief — three EMAs is the typical
   * fast/slow/signal triple and shouldn't be visually flattened.
   */
  groupedEntriesView = computed<EntriesViewBlock[]>(() => {
    const items = this.sortedEntriesView();
    const out: EntriesViewBlock[] = [];
    let i = 0;
    while (i < items.length) {
      const name = items[i].entry.name;
      let j = i;
      while (j < items.length && items[j].entry.name === name) j++;
      const run = items.slice(i, j);
      if (run.length >= 4) {
        out.push({ kind: 'group', name, items: run });
      } else {
        for (const it of run) out.push({ kind: 'single', item: it });
      }
      i = j;
    }
    return out;
  });

  /** Reset every entry sharing a name to its INDICATOR_CONFIGS defaults. */
  resetGroupToDefaults(name: string): void {
    const info = this.indicatorMap()[name];
    if (!info) return;
    const defaults: Record<string, number> = {};
    for (const p of info.configurable_params) defaults[p.name] = p.default;
    this.entries.update((list) =>
      list.map((e) => (e.name === name ? { ...e, params: { ...defaults } } : e)),
    );
  }

  /** Remove every entry sharing a name. Used by the group card's
   *  "Remove all" affordance. */
  removeGroupByName(name: string): void {
    this.entries.update((list) => list.filter((e) => e.name !== name));
  }

  /** Scroll the page to the Indicator Catalog section. Used by the
   *  "+ Add indicator" button in the Active Indicators sub-toolbar. */
  scrollToCatalog(): void {
    const el = document.getElementById('data-lab-indicator-catalog');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /** Increment/decrement strikes-each-side from the Companion plan card.
   *  Clamped to [1, 25] to match the `<input>` bounds in the form. */
  adjustStrikes(delta: number): void {
    const next = Math.max(1, Math.min(25, this.optionsStrikesEachSide() + delta));
    this.optionsStrikesEachSide.set(next);
  }

  /** Quick-action for daily-expiry tickers in warn/danger: jump to a
   *  weekly-cadence DTE distance so most listed expiries skip past
   *  daily ones, reducing matched trading days roughly 5×. */
  switchToWeeklyOnly(): void {
    this.optionsDteDistance.set(7);
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
        include_previous_close: this.includePreviousClose(),
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
            symbol: this.ticker(),
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
