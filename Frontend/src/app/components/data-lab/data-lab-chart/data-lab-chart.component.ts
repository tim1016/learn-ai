import {
  Component, signal, computed, inject, input, output,
  viewChild, ElementRef, AfterViewInit, OnDestroy,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, CandlestickData,
  HistogramSeries, HistogramData,
  LineSeries, LineData,
  UTCTimestamp,
} from 'lightweight-charts';
import { environment } from '../../../../environments/environment';
import { QualityModalComponent } from '../quality-modal/quality-modal.component';

// ──────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────
export interface ChartBar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  session?: string;
  synthetic?: boolean;
}

export interface IndicatorPoint {
  t: number;
  value: number | null;
}

export interface ChartIndicatorResult {
  id: string;
  panel: string;
  type: string;
  color: string;
  data: IndicatorPoint[] | Record<string, IndicatorPoint[]>;
  refs: number[];
  default_visible?: boolean;
}

export interface GapDetail {
  before_ts: number;
  after_ts: number;
  duration_minutes: number;
  classification?: string;
}

export interface QualityReport {
  raw_bar_count: number;
  resampled_bar_count: number;
  duplicates_removed: number;
  gaps_found: number;
  largest_gap_minutes: number;
  missing_sessions: number;
  session_coverage_pct: number;
  synthetic_bars: number;
  gap_details: GapDetail[];
  missing_session_dates: string[];
  flat_bars_detected?: number;
  ohlc_violations_detected?: number;
  out_of_order_fixed?: number;
}

export interface ChartDataResponse {
  bars: ChartBar[];
  indicators: ChartIndicatorResult[];
  quality: QualityReport;
  allowed_timeframes: string[];
  estimated_bars_per_timeframe: Record<string, number>;
  recommended_timeframe: string;
  meta: { cached_resample: boolean; cached_indicators: boolean };
}

export interface ChartIndicatorEntry {
  name: string;
  params: Record<string, number>;
}

interface SubPanel {
  id: string;
  container: HTMLDivElement;
  chart: IChartApi;
  seriesMap: Map<string, ISeriesApi<any>>;
}

const ALL_TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1D', '1W', '1M'];

// ──────────────────────────────────────────────
// Dark theme palette
// ──────────────────────────────────────────────
const DARK = {
  bg:         '#0f1117',
  surface:    '#161922',
  grid:       'rgba(42, 46, 62, 0.5)',
  text:       '#8892a8',
  border:     '#2a2e3e',
  crosshair:  '#4a5068',
  bull:       '#00c896',
  bear:       '#e5334e',
  bullMuted:  'rgba(0, 200, 150, 0.35)',
  bearMuted:  'rgba(229, 51, 78, 0.35)',
  bullVolume: 'rgba(0, 200, 150, 0.20)',
  bearVolume: 'rgba(229, 51, 78, 0.20)',
  refLine:    '#3b4560',
};

// EMA rainbow ribbon — brighter = shorter period, deeper = longer
const EMA_RIBBON: Record<number, string> = {
  5:   '#ffeb3b',  // bright yellow
  8:   '#fdd835',
  9:   '#fdd835',
  10:  '#ffc107',  // amber
  12:  '#ffb300',
  13:  '#ffa000',
  20:  '#ff9800',  // orange
  21:  '#ff8f00',
  26:  '#ff6d00',
  30:  '#ff5722',  // deep orange
  40:  '#e91e63',  // pink
  50:  '#9c27b0',  // purple
  55:  '#8e24aa',
  100: '#3f51b5',  // indigo
  200: '#2196f3',  // blue
};

/** Get the EMA ribbon color for a given length, or interpolate. */
function getEmaRibbonColor(length: number): string | null {
  if (EMA_RIBBON[length]) return EMA_RIBBON[length];

  // Interpolate between known breakpoints
  const keys = Object.keys(EMA_RIBBON).map(Number).sort((a, b) => a - b);
  if (length < keys[0]) return EMA_RIBBON[keys[0]];
  if (length > keys[keys.length - 1]) return EMA_RIBBON[keys[keys.length - 1]];

  for (let i = 0; i < keys.length - 1; i++) {
    if (length >= keys[i] && length <= keys[i + 1]) {
      // Simple: pick the nearest
      const mid = (keys[i] + keys[i + 1]) / 2;
      return length <= mid ? EMA_RIBBON[keys[i]] : EMA_RIBBON[keys[i + 1]];
    }
  }
  return null;
}

@Component({
  selector: 'app-data-lab-chart',
  standalone: true,
  imports: [CommonModule, QualityModalComponent],
  templateUrl: './data-lab-chart.component.html',
  styleUrls: ['./data-lab-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabChartComponent implements AfterViewInit, OnDestroy {
  private http = inject(HttpClient);

  // Inputs from parent (shared controls). The chart is purely a renderer
  // for the parent's chosen (ticker, range, timeframe) — it no longer owns
  // its own timeframe selector. The parent is the source of truth and
  // applies any auto-cap logic before the fetch.
  ticker = input.required<string>();
  fromDate = input.required<string>();
  toDate = input.required<string>();
  session = input.required<string>();
  forwardFill = input.required<boolean>();
  timeframe = input.required<string>();
  adjusted = input(true);
  chartIndicators = input<ChartIndicatorEntry[]>([]);
  computeAllIndicators = input(false);

  // Outputs
  /** Emitted after chart data is fetched (or loaded from cache) so parent can snapshot it. */
  dataLoaded = output<{
    bars: ChartBar[];
    indicators: ChartIndicatorResult[];
    quality: QualityReport;
    allowedTimeframes: string[];
    estimatedBarsPerTimeframe: Record<string, number>;
    recommendedTimeframe: string;
    visibleIndicatorIds: string[];
    timeframe: string;
  }>();

  /** Emitted when the chart endpoint rejects the request with a
   *  TIMEFRAME_NOT_ALLOWED error and supplies a recommendation. The
   *  parent owns the timeframe and decides whether to apply it. This
   *  closes the gap between the parent's safety-net threshold (250k
   *  bars) and the chart endpoint's stricter ~20k limit — without it,
   *  manual picks in the 20k–250k range fail with no auto-correction. */
  timeframeRejected = output<{
    requested: string;
    recommended: string;
    detail: string;
  }>();

  // Chart container refs
  mainChartContainer = viewChild<ElementRef<HTMLDivElement>>('mainChart');
  subPanelHost = viewChild<ElementRef<HTMLDivElement>>('subPanelHost');

  // State
  allowedTimeframes = signal<string[]>([...ALL_TIMEFRAMES]);
  estimatedBars = signal<Record<string, number>>({});
  recommendedTimeframe = signal('1D');
  loading = signal(false);
  error = signal('');
  toastMessage = signal('');

  quality = signal<QualityReport | null>(null);
  qualityModalOpen = signal(false);

  // Chart data
  private bars = signal<ChartBar[]>([]);
  indicatorResults = signal<ChartIndicatorResult[]>([]);

  // Indicator visibility (Set of indicator IDs that are visible)
  visibleIndicators = signal<Set<string>>(new Set());

  // Indicator chips for the toolbar — use the actual rendered color (e.g. EMA ribbon)
  indicatorChips = computed(() => {
    const results = this.indicatorResults();
    return results.map(r => ({
      id: r.id,
      panel: r.panel,
      color: this.getRenderedColor(r),
      visible: this.visibleIndicators().has(r.id),
    }));
  });

  get hasIndicators(): boolean {
    return this.indicatorResults().length > 0;
  }

  /** Resolve the actual color used when rendering a series (e.g. EMA ribbon override). */
  private getRenderedColor(ind: ChartIndicatorResult): string {
    const emaMatch = ind.id.match(/^ema[_.](\d+)/i);
    if (emaMatch) {
      return getEmaRibbonColor(parseInt(emaMatch[1], 10)) ?? ind.color;
    }
    return ind.color;
  }

  // Chart instances
  private mainChart: IChartApi | null = null;
  private candleSeries: ISeriesApi<'Candlestick'> | null = null;
  private volumeSeries: ISeriesApi<'Histogram'> | null = null;
  private overlaySeries = new Map<string, ISeriesApi<'Line'>>();
  private subPanels: SubPanel[] = [];
  private resizeObserver: ResizeObserver | null = null;
  private _isSyncing = false;
  private initialized = false;

  // Computed
  hasData = computed(() => this.bars().length > 0);

  ngAfterViewInit(): void {
    this.createMainChart();
    this.initialized = true;
    // No auto-fetch here. The parent decides when to fetch (live click) vs
    // when to load from a cached session via loadCachedData(); auto-fetching
    // on mount would race the cache restore and double-spend Polygon calls.
  }

  /** Called by parent when user clicks "Fetch Data". The parent owns the
   *  timeframe — there is no precheck or auto-switch here. If the chart
   *  endpoint rejects the request (e.g. far too many bars), the parent's
   *  bar-count safety net should have already pulled the timeframe back. */
  fetchData(): void {
    if (!this.ticker() || !this.fromDate() || !this.toDate()) return;
    this.fetchChartData();
  }

  /**
   * Restore chart from a cached snapshot — no API call needed.
   * Called by parent when loading a saved session.
   */
  loadCachedData(snapshot: {
    bars: ChartBar[];
    indicators: ChartIndicatorResult[];
    quality: QualityReport;
    allowedTimeframes: string[];
    estimatedBarsPerTimeframe: Record<string, number>;
    recommendedTimeframe: string;
    visibleIndicatorIds: string[];
    timeframe: string;
  }): void {
    this.bars.set(snapshot.bars);
    this.indicatorResults.set(snapshot.indicators);
    this.quality.set(snapshot.quality);
    this.allowedTimeframes.set(snapshot.allowedTimeframes);
    this.estimatedBars.set(snapshot.estimatedBarsPerTimeframe);
    this.recommendedTimeframe.set(snapshot.recommendedTimeframe);
    // Note: snapshot.timeframe is informational here — the parent already
    // mirrored it into its own timespan/multiplier signals before calling.
    this.visibleIndicators.set(new Set(snapshot.visibleIndicatorIds));
    this.error.set('');

    // Wait a tick for the container to display, then render
    setTimeout(() => {
      this.renderMainChart();
      this.renderOverlays();
      this.renderSubPanels();
    });
  }

  ngOnDestroy(): void {
    this.resizeObserver?.disconnect();
    this.destroyAllSubPanels();
    this.overlaySeries.clear();
    this.mainChart?.remove();
  }

  // ──────────────────────────────────────────────
  // Data fetching
  // ──────────────────────────────────────────────

  private async fetchChartData(): Promise<void> {
    const ticker = this.ticker();
    const fromDate = this.fromDate();
    const toDate = this.toDate();
    if (!ticker || !fromDate || !toDate) return;

    this.loading.set(true);
    this.error.set('');

    try {
      const computeAll = this.computeAllIndicators();
      const indicators = computeAll ? [] : this.chartIndicators().map(i => ({
        name: i.name,
        params: i.params,
      }));

      const resp = await firstValueFrom(
        this.http.post<ChartDataResponse>(
          `${environment.pythonServiceUrl}/api/chart/data`,
          {
            ticker,
            from_date: fromDate,
            to_date: toDate,
            timeframe: this.timeframe(),
            session: this.session(),
            forward_fill: this.forwardFill(),
            adjusted: this.adjusted(),
            indicators,
            compute_all_indicators: computeAll,
          }
        )
      );

      this.bars.set(resp.bars);
      this.indicatorResults.set(resp.indicators);
      this.quality.set(resp.quality);
      this.allowedTimeframes.set(resp.allowed_timeframes);
      this.estimatedBars.set(resp.estimated_bars_per_timeframe);
      this.recommendedTimeframe.set(resp.recommended_timeframe);

      // When compute_all_indicators is on, only show default_visible indicators.
      // Otherwise, show all indicators (existing behavior).
      const visibleIds = computeAll
        ? resp.indicators.filter(i => i.default_visible).map(i => i.id)
        : resp.indicators.map(i => i.id);
      this.visibleIndicators.set(new Set(visibleIds));

      this.renderMainChart();
      this.renderOverlays();
      this.renderSubPanels();

      // Notify parent so it can snapshot the data for session persistence
      this.dataLoaded.emit({
        bars: resp.bars,
        indicators: resp.indicators,
        quality: resp.quality,
        allowedTimeframes: resp.allowed_timeframes,
        estimatedBarsPerTimeframe: resp.estimated_bars_per_timeframe,
        recommendedTimeframe: resp.recommended_timeframe,
        visibleIndicatorIds: visibleIds,
        timeframe: this.timeframe(),
      });
    } catch (e: any) {
      const detail = e?.error?.detail;
      if (detail && typeof detail === 'object' && detail.error_code) {
        switch (detail.error_code) {
          case 'TIMEFRAME_NOT_ALLOWED':
            this.error.set(detail.detail);
            if (detail.allowed_timeframes) {
              this.allowedTimeframes.set(detail.allowed_timeframes);
            }
            // The parent owns the timeframe selection. Hand it the
            // recommendation so it can auto-correct: the parent's bar-count
            // safety net only fires above 250k expected bars, but the chart
            // endpoint rejects at ~20k, so without this the 20k–250k range
            // would consistently fail with no auto-recovery.
            if (detail.recommended_timeframe) {
              this.timeframeRejected.emit({
                requested: this.timeframe(),
                recommended: detail.recommended_timeframe,
                detail: detail.detail,
              });
              this.toastMessage.set(`Switched to ${detail.recommended_timeframe} for this range`);
              setTimeout(() => this.toastMessage.set(''), 4000);
            }
            break;
          case 'NO_DATA':
            this.error.set(`No data for ${ticker} in this range`);
            break;
          case 'RATE_LIMITED':
            this.error.set('Rate limited — please wait a moment and try again');
            break;
          default:
            this.error.set(detail.detail || 'An error occurred');
        }
      } else {
        this.error.set(e?.message || 'Failed to fetch chart data');
      }
    } finally {
      this.loading.set(false);
    }
  }

  // ──────────────────────────────────────────────
  // Main chart creation
  // ──────────────────────────────────────────────
  private createMainChart(): void {
    const el = this.mainChartContainer()?.nativeElement;
    if (!el) return;

    this.mainChart = createChart(el, {
      width: el.clientWidth,
      height: 560,
      layout: {
        background: { color: DARK.bg },
        textColor: DARK.text,
      },
      grid: {
        vertLines: { color: DARK.grid },
        horzLines: { color: DARK.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: DARK.border,
        minBarSpacing: 0.5,
      },
      crosshair: {
        mode: 0,
        vertLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
        horzLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
      },
      rightPriceScale: {
        borderColor: DARK.border,
      },
    });

    this.candleSeries = this.mainChart.addSeries(CandlestickSeries, {
      upColor: DARK.bull,
      downColor: DARK.bear,
      borderUpColor: DARK.bull,
      borderDownColor: DARK.bear,
      wickUpColor: DARK.bull,
      wickDownColor: DARK.bear,
    });

    this.volumeSeries = this.mainChart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });

    this.mainChart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    // Sync sub-panels on visible range change
    this.mainChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (this._isSyncing || !range) return;
      this._isSyncing = true;
      requestAnimationFrame(() => {
        for (const panel of this.subPanels) {
          panel.chart.timeScale().setVisibleLogicalRange(range);
        }
        this._isSyncing = false;
      });
    });

    // Responsive resize
    this.resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0) {
        const { width } = entries[0].contentRect;
        this.mainChart?.applyOptions({ width });
        for (const panel of this.subPanels) {
          panel.chart.applyOptions({ width });
        }
      }
    });
    this.resizeObserver.observe(el);
  }

  // ──────────────────────────────────────────────
  // Render candles + volume
  // ──────────────────────────────────────────────
  private renderMainChart(): void {
    if (!this.candleSeries || !this.volumeSeries) return;

    const bars = this.bars();
    if (!bars.length) return;

    const candleData: CandlestickData[] = bars.map(bar => {
      const isRth = bar.session === 'rth';
      const up = (bar.c ?? 0) >= (bar.o ?? 0);

      // Muted colors for pre/post-market (extended hours)
      if (!isRth && bar.session) {
        return {
          time: (bar.t / 1000) as UTCTimestamp,
          open: bar.o,
          high: bar.h,
          low: bar.l,
          close: bar.c,
          color: up ? DARK.bullMuted : DARK.bearMuted,
          borderColor: up ? DARK.bullMuted : DARK.bearMuted,
          wickColor: up ? DARK.bullMuted : DARK.bearMuted,
        } as CandlestickData;
      }

      return {
        time: (bar.t / 1000) as UTCTimestamp,
        open: bar.o,
        high: bar.h,
        low: bar.l,
        close: bar.c,
      } as CandlestickData;
    }).sort((a, b) => (a.time as number) - (b.time as number));

    this.candleSeries.setData(candleData);

    const volumeData: HistogramData[] = bars.map(bar => ({
      time: (bar.t / 1000) as UTCTimestamp,
      value: bar.v ?? 0,
      color: (bar.c ?? 0) >= (bar.o ?? 0) ? DARK.bullVolume : DARK.bearVolume,
    })).sort((a, b) => (a.time as number) - (b.time as number));

    this.volumeSeries.setData(volumeData);

    // Set visible range to actual data bounds, clamped to the input date range
    const inputFrom = new Date(this.fromDate()).getTime() / 1000;
    const inputTo = new Date(this.toDate() + 'T23:59:59').getTime() / 1000;
    const dataFrom = candleData[0].time as number;
    const dataTo = candleData[candleData.length - 1].time as number;
    this.mainChart?.timeScale().setVisibleRange({
      from: Math.max(inputFrom, dataFrom) as UTCTimestamp,
      to: Math.min(inputTo, dataTo) as UTCTimestamp,
    });
  }

  // ──────────────────────────────────────────────
  // Overlay indicators on main chart
  // ──────────────────────────────────────────────
  private renderOverlays(): void {
    if (!this.mainChart) return;

    // Remove existing overlays
    for (const [, series] of this.overlaySeries) {
      this.mainChart.removeSeries(series);
    }
    this.overlaySeries.clear();

    const results = this.indicatorResults();
    const overlayResults = results.filter(r => r.panel === 'main');

    for (const ind of overlayResults) {
      if (ind.type === 'line' && Array.isArray(ind.data)) {
        const lineData: LineData[] = (ind.data as IndicatorPoint[])
          .filter(p => p.value !== null)
          .map(p => ({
            time: (p.t / 1000) as UTCTimestamp,
            value: p.value!,
          }))
          .sort((a, b) => (a.time as number) - (b.time as number));

        if (lineData.length === 0) continue;

        // EMA rainbow ribbon: override color for EMA indicators based on length
        let color = ind.color;
        const emaMatch = ind.id.match(/^ema[_.](\d+)/i);
        if (emaMatch) {
          const len = parseInt(emaMatch[1], 10);
          color = getEmaRibbonColor(len) ?? ind.color;
        }

        const isBband = ind.id.includes('bbands');
        const series = this.mainChart.addSeries(LineSeries, {
          color,
          lineWidth: isBband ? 1 : 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        series.setData(lineData);
        this.overlaySeries.set(ind.id, series);
      }
    }
  }

  // ──────────────────────────────────────────────
  // Sub-panels (oscillators)
  // ──────────────────────────────────────────────
  private renderSubPanels(): void {
    this.destroyAllSubPanels();

    const results = this.indicatorResults();
    const hostEl = this.subPanelHost()?.nativeElement;
    if (!hostEl) return;

    // Group by panel
    const panelGroups = new Map<string, ChartIndicatorResult[]>();
    for (const ind of results) {
      if (ind.panel === 'main') continue;
      const existing = panelGroups.get(ind.panel) ?? [];
      existing.push(ind);
      panelGroups.set(ind.panel, existing);
    }

    for (const [panelId, indicators] of panelGroups) {
      this.createSubPanel(hostEl, panelId, indicators);
    }
  }

  private createSubPanel(
    host: HTMLDivElement,
    panelId: string,
    indicators: ChartIndicatorResult[]
  ): void {
    // Create container
    const container = document.createElement('div');
    container.className = 'sub-panel';
    container.style.width = '100%';
    container.style.height = '150px';
    container.style.marginTop = '4px';
    container.style.position = 'relative';

    // Panel label
    const label = document.createElement('div');
    label.className = 'sub-panel-label';
    label.textContent = panelId.toUpperCase();

    // Close button
    const closeBtn = document.createElement('button');
    closeBtn.className = 'sub-panel-close';
    closeBtn.innerHTML = '&times;';
    closeBtn.onclick = () => this.removeSubPanel(panelId);
    label.appendChild(closeBtn);

    container.appendChild(label);
    host.appendChild(container);

    const chart = createChart(container, {
      width: container.clientWidth,
      height: 150,
      layout: {
        background: { color: DARK.bg },
        textColor: DARK.text,
      },
      grid: {
        vertLines: { color: DARK.grid },
        horzLines: { color: DARK.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: DARK.border,
        visible: false,
      },
      crosshair: {
        mode: 0,
        vertLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
        horzLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
      },
      rightPriceScale: {
        borderColor: DARK.border,
      },
    });

    // Disable animations
    chart.timeScale().applyOptions({
      shiftVisibleRangeOnNewBar: false,
    });

    const seriesMap = new Map<string, ISeriesApi<any>>();

    for (const ind of indicators) {
      if (ind.type === 'macd' && !Array.isArray(ind.data)) {
        const macdData = ind.data as Record<string, IndicatorPoint[]>;

        // Histogram — gradient opacity based on momentum
        if (macdData['histogram']) {
          const histSeries = chart.addSeries(HistogramSeries, {
            priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
          });
          const sorted = macdData['histogram']
            .filter(p => p.value !== null)
            .sort((a, b) => a.t - b.t);

          const histData: HistogramData[] = sorted.map((p, i) => {
            const val = p.value!;
            const prev = i > 0 ? sorted[i - 1].value! : 0;
            const isGrowing = Math.abs(val) >= Math.abs(prev);
            const opacity = isGrowing ? 0.85 : 0.35;
            const color = val >= 0
              ? `rgba(0, 200, 150, ${opacity})`
              : `rgba(229, 51, 78, ${opacity})`;
            return {
              time: (p.t / 1000) as UTCTimestamp,
              value: val,
              color,
            };
          });
          histSeries.setData(histData);
          seriesMap.set(`${ind.id}_hist`, histSeries);
        }

        // MACD line
        if (macdData['macd']) {
          const macdSeries = chart.addSeries(LineSeries, {
            color: '#3b82f6',
            lineWidth: 2,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          const lineData: LineData[] = macdData['macd']
            .filter(p => p.value !== null)
            .map(p => ({ time: (p.t / 1000) as UTCTimestamp, value: p.value! }))
            .sort((a, b) => (a.time as number) - (b.time as number));
          macdSeries.setData(lineData);
          seriesMap.set(`${ind.id}_macd`, macdSeries);
        }

        // Signal line
        if (macdData['signal']) {
          const signalSeries = chart.addSeries(LineSeries, {
            color: '#f59e0b',
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          const lineData: LineData[] = macdData['signal']
            .filter(p => p.value !== null)
            .map(p => ({ time: (p.t / 1000) as UTCTimestamp, value: p.value! }))
            .sort((a, b) => (a.time as number) - (b.time as number));
          signalSeries.setData(lineData);
          seriesMap.set(`${ind.id}_signal`, signalSeries);
        }
      } else if (ind.type === 'line' && Array.isArray(ind.data)) {
        const lineSeries = chart.addSeries(LineSeries, {
          color: ind.color,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        const lineData: LineData[] = (ind.data as IndicatorPoint[])
          .filter(p => p.value !== null)
          .map(p => ({ time: (p.t / 1000) as UTCTimestamp, value: p.value! }))
          .sort((a, b) => (a.time as number) - (b.time as number));
        lineSeries.setData(lineData);
        seriesMap.set(ind.id, lineSeries);

        // Reference lines
        if (ind.refs?.length) {
          for (const refVal of ind.refs) {
            lineSeries.createPriceLine({
              price: refVal,
              color: DARK.refLine,
              lineWidth: 1,
              lineStyle: 2, // dashed
              axisLabelVisible: true,
            });
          }
        }
      }
    }

    // Sync: match main chart visible range
    if (this.mainChart) {
      const mainRange = this.mainChart.timeScale().getVisibleLogicalRange();
      if (mainRange) {
        chart.timeScale().setVisibleLogicalRange(mainRange);
      }
    }

    this.subPanels.push({ id: panelId, container, chart, seriesMap });
  }

  private removeSubPanel(panelId: string): void {
    const idx = this.subPanels.findIndex(p => p.id === panelId);
    if (idx === -1) return;
    const panel = this.subPanels[idx];
    panel.chart.remove();
    panel.container.remove();
    this.subPanels.splice(idx, 1);
  }

  private destroyAllSubPanels(): void {
    for (const panel of this.subPanels) {
      panel.chart.remove();
      panel.container.remove();
    }
    this.subPanels = [];
  }

  // ──────────────────────────────────────────────
  // Indicator visibility toggle
  // ──────────────────────────────────────────────
  toggleIndicatorVisibility(id: string): void {
    this.visibleIndicators.update(set => {
      const next = new Set(set);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
    this.applyVisibility(id);
  }

  toggleAllIndicators(visible: boolean): void {
    if (visible) {
      this.visibleIndicators.set(new Set(this.indicatorResults().map(i => i.id)));
    } else {
      this.visibleIndicators.set(new Set());
    }
    // Re-apply all visibility
    for (const ind of this.indicatorResults()) {
      this.applyVisibility(ind.id);
    }
  }

  private applyVisibility(id: string): void {
    const isVisible = this.visibleIndicators().has(id);
    const ind = this.indicatorResults().find(r => r.id === id);
    if (!ind) return;

    if (ind.panel === 'main') {
      // Overlay: toggle series visibility
      const series = this.overlaySeries.get(id);
      if (series) {
        series.applyOptions({ visible: isVisible });
      }
    } else {
      // Sub-panel: show/hide entire panel if all indicators in it are hidden/shown
      this.syncSubPanelVisibility(ind.panel);
    }
  }

  private syncSubPanelVisibility(panelId: string): void {
    const visible = this.visibleIndicators();
    const panelIndicators = this.indicatorResults().filter(r => r.panel === panelId);
    const anyVisible = panelIndicators.some(r => visible.has(r.id));

    const existingPanel = this.subPanels.find(p => p.id === panelId);

    if (anyVisible && !existingPanel) {
      // Need to create the panel
      const hostEl = this.subPanelHost()?.nativeElement;
      if (hostEl) {
        this.createSubPanel(hostEl, panelId, panelIndicators.filter(r => visible.has(r.id)));
        // Sync range from main chart
        if (this.mainChart) {
          const mainRange = this.mainChart.timeScale().getVisibleLogicalRange();
          const newPanel = this.subPanels.find(p => p.id === panelId);
          if (mainRange && newPanel) {
            newPanel.chart.timeScale().setVisibleLogicalRange(mainRange);
          }
        }
      }
    } else if (!anyVisible && existingPanel) {
      // Remove the panel entirely
      this.removeSubPanel(panelId);
    } else if (anyVisible && existingPanel) {
      // Toggle individual series within the panel
      for (const ind of panelIndicators) {
        const isVis = visible.has(ind.id);
        // For MACD, toggle all sub-series
        if (ind.type === 'macd') {
          for (const suffix of ['_hist', '_macd', '_signal']) {
            const s = existingPanel.seriesMap.get(`${ind.id}${suffix}`);
            if (s) s.applyOptions({ visible: isVis });
          }
        } else {
          const s = existingPanel.seriesMap.get(ind.id);
          if (s) s.applyOptions({ visible: isVis });
        }
      }
    }
  }

  // ──────────────────────────────────────────────
  // Quality report toggle
  // ──────────────────────────────────────────────
  toggleQuality(): void {
    this.qualityModalOpen.update(v => !v);
  }
}
