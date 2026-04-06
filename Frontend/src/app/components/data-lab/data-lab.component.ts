import {
  Component, signal, computed, inject, viewChild,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { DatePicker } from 'primeng/datepicker';
import { SharedModule } from 'primeng/api';
import { Tooltip } from 'primeng/tooltip';
import { environment } from '../../../environments/environment';
import { DataLabChartComponent, ChartIndicatorEntry } from './data-lab-chart/data-lab-chart.component';
import { DataLabSessionService, DataLabSessionSummary, DataLabSessionChartSnapshot } from '../../services/data-lab-session.service';
import { MarketMonitorService } from '../../services/market-monitor.service';
import { MarketHolidayEvent } from '../../models/market-monitor';
import {
  getDisabledHolidayDates,
  buildHolidayMap,
  getMinAllowedDate,
  validateDateRange,
} from '../../utils/date-validation';

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
  imports: [CommonModule, FormsModule, RouterModule, DataLabChartComponent, DatePicker, SharedModule, Tooltip],
  templateUrl: './data-lab.component.html',
  styleUrls: ['./data-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabComponent {
  private http = inject(HttpClient);
  private sessionService = inject(DataLabSessionService);
  private marketMonitor = inject(MarketMonitorService);

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
  warmup = signal(true);
  computeAllIndicators = signal(false);
  timespan = signal<'minute' | 'hour' | 'day'>('minute');
  multiplier = signal(1);

  loading = signal(false);
  loadingIndicators = signal(false);
  loadingValidation = signal(false);
  error = signal('');
  progress = signal('');

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

  // Selected indicator names (unique set for checkbox state)
  get selectedNames(): Set<string> {
    return new Set(this.entries().map(e => e.name));
  }

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
    this.loadAvailableIndicators();
    this.refreshSessionList();
    this.loadHolidays();
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
      next.has(catName) ? next.delete(catName) : next.add(catName);
      return next;
    });
  }

  isCategoryExpanded(catName: string): boolean {
    return this.expandedCategories().has(catName);
  }

  isSelected(name: string): boolean {
    return this.entries().some(e => e.name === name);
  }

  categorySelectedCount(catName: string): number {
    const cat = this.categories().find(c => c.name === catName);
    if (!cat) return 0;
    const names = this.selectedNames;
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
      const payload = {
        ticker: this.ticker(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        indicator_entries: this.entries(),
        session: this.session(),
        forward_fill: this.forwardFill(),
        warmup: this.warmup(),
        timespan: this.timespan(),
        multiplier: this.multiplier(),
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

  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}
