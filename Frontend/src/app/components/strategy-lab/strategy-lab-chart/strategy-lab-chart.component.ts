import {
  Component, signal, computed, input, output,
  viewChild, ElementRef, AfterViewInit, OnDestroy,
  ChangeDetectionStrategy, effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, CandlestickData,
  HistogramSeries, HistogramData,
  LineSeries, LineData,
  UTCTimestamp,
  createSeriesMarkers, ISeriesMarkersPluginApi,
} from 'lightweight-charts';
import { QualityModalComponent } from '../../data-lab/quality-modal/quality-modal.component';

// ──────────────────────────────────────────────
// Types (duplicated from DataLabChart for independence)
// ──────────────────────────────────────────────
export interface ChartBar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  session?: string;
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

export interface BacktestTradeForChart {
  entry_timestamp: string;
  exit_timestamp: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  trade_type: string;
  signal_reason: string;
}

interface SubPanel {
  id: string;
  container: HTMLDivElement;
  chart: IChartApi;
  seriesMap: Map<string, ISeriesApi<any>>;
}

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

const EMA_RIBBON: Record<number, string> = {
  5:   '#ffeb3b',
  8:   '#fdd835',
  9:   '#fdd835',
  10:  '#ffc107',
  12:  '#ffb300',
  13:  '#ffa000',
  20:  '#ff9800',
  21:  '#ff8f00',
  26:  '#ff6d00',
  30:  '#ff5722',
  40:  '#e91e63',
  50:  '#9c27b0',
  55:  '#8e24aa',
  100: '#3f51b5',
  200: '#2196f3',
};

function getEmaRibbonColor(length: number): string | null {
  if (EMA_RIBBON[length]) return EMA_RIBBON[length];
  const keys = Object.keys(EMA_RIBBON).map(Number).sort((a, b) => a - b);
  if (length < keys[0]) return EMA_RIBBON[keys[0]];
  if (length > keys[keys.length - 1]) return EMA_RIBBON[keys[keys.length - 1]];
  for (let i = 0; i < keys.length - 1; i++) {
    if (length >= keys[i] && length <= keys[i + 1]) {
      const mid = (keys[i] + keys[i + 1]) / 2;
      return length <= mid ? EMA_RIBBON[keys[i]] : EMA_RIBBON[keys[i + 1]];
    }
  }
  return null;
}

@Component({
  selector: 'app-strategy-lab-chart',
  standalone: true,
  imports: [CommonModule, QualityModalComponent],
  templateUrl: './strategy-lab-chart.component.html',
  styleUrls: ['./strategy-lab-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabChartComponent implements AfterViewInit, OnDestroy {
  // Inputs — data is pre-fetched by parent (backtest response)
  chartBars = input<ChartBar[]>([]);
  chartIndicators = input<ChartIndicatorResult[]>([]);
  quality = input<QualityReport | null>(null);
  trades = input<BacktestTradeForChart[]>([]);
  fromDate = input<string>('');
  toDate = input<string>('');

  // Chart container refs
  mainChartContainer = viewChild<ElementRef<HTMLDivElement>>('mainChart');
  subPanelHost = viewChild<ElementRef<HTMLDivElement>>('subPanelHost');

  // State
  qualityModalOpen = signal(false);

  // Indicator visibility
  visibleIndicators = signal<Set<string>>(new Set());

  indicatorChips = computed(() => {
    const results = this.chartIndicators();
    return results.map(r => ({
      id: r.id,
      panel: r.panel,
      color: this.getRenderedColor(r),
      visible: this.visibleIndicators().has(r.id),
    }));
  });

  get hasIndicators(): boolean {
    return this.chartIndicators().length > 0;
  }

  hasData = computed(() => this.chartBars().length > 0);

  // Chart instances
  private mainChart: IChartApi | null = null;
  private candleSeries: ISeriesApi<'Candlestick'> | null = null;
  private volumeSeries: ISeriesApi<'Histogram'> | null = null;
  private overlaySeries: Map<string, ISeriesApi<'Line'>> = new Map();
  private subPanels: SubPanel[] = [];
  private markersPlugin: ISeriesMarkersPluginApi<any> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private _isSyncing = false;
  private initialized = false;

  constructor() {
    // Re-render when input data changes
    effect(() => {
      const bars = this.chartBars();
      const indicators = this.chartIndicators();
      if (bars.length > 0 && this.initialized) {
        // Show all indicators by default
        this.visibleIndicators.set(new Set(indicators.map(i => i.id)));
        setTimeout(() => {
          this.renderMainChart();
          this.renderOverlays();
          this.renderTradeMarkers();
          this.renderSubPanels();
        });
      }
    });
  }

  ngAfterViewInit(): void {
    this.createMainChart();
    this.initialized = true;

    // Render if data was already available before init
    if (this.chartBars().length > 0) {
      this.visibleIndicators.set(new Set(this.chartIndicators().map(i => i.id)));
      setTimeout(() => {
        this.renderMainChart();
        this.renderOverlays();
        this.renderTradeMarkers();
        this.renderSubPanels();
      });
    }
  }

  ngOnDestroy(): void {
    this.resizeObserver?.disconnect();
    this.markersPlugin?.detach();
    this.destroyAllSubPanels();
    this.overlaySeries.clear();
    this.mainChart?.remove();
  }

  // ──────────────────────────────────────────────
  // Color helpers
  // ──────────────────────────────────────────────
  private getRenderedColor(ind: ChartIndicatorResult): string {
    const emaMatch = ind.id.match(/^ema[_.](\d+)/i);
    if (emaMatch) {
      return getEmaRibbonColor(parseInt(emaMatch[1], 10)) ?? ind.color;
    }
    return ind.color;
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

    const bars = this.chartBars();
    if (!bars.length) return;

    const candleData: CandlestickData[] = bars.map(bar => {
      const isRth = bar.session === 'rth';
      const up = (bar.c ?? 0) >= (bar.o ?? 0);

      if (!isRth && bar.session) {
        return {
          time: (bar.t / 1000) as UTCTimestamp,
          open: bar.o, high: bar.h, low: bar.l, close: bar.c,
          color: up ? DARK.bullMuted : DARK.bearMuted,
          borderColor: up ? DARK.bullMuted : DARK.bearMuted,
          wickColor: up ? DARK.bullMuted : DARK.bearMuted,
        } as CandlestickData;
      }

      return {
        time: (bar.t / 1000) as UTCTimestamp,
        open: bar.o, high: bar.h, low: bar.l, close: bar.c,
      } as CandlestickData;
    }).sort((a, b) => (a.time as number) - (b.time as number));

    this.candleSeries.setData(candleData);

    const volumeData: HistogramData[] = bars.map(bar => ({
      time: (bar.t / 1000) as UTCTimestamp,
      value: bar.v ?? 0,
      color: (bar.c ?? 0) >= (bar.o ?? 0) ? DARK.bullVolume : DARK.bearVolume,
    })).sort((a, b) => (a.time as number) - (b.time as number));

    this.volumeSeries.setData(volumeData);

    // Set visible range
    const fromDate = this.fromDate();
    const toDate = this.toDate();
    if (fromDate && toDate) {
      const inputFrom = new Date(fromDate).getTime() / 1000;
      const inputTo = new Date(toDate + 'T23:59:59').getTime() / 1000;
      const dataFrom = candleData[0].time as number;
      const dataTo = candleData[candleData.length - 1].time as number;
      this.mainChart?.timeScale().setVisibleRange({
        from: Math.max(inputFrom, dataFrom) as UTCTimestamp,
        to: Math.min(inputTo, dataTo) as UTCTimestamp,
      });
    }
  }

  // ──────────────────────────────────────────────
  // Trade markers overlay
  // ──────────────────────────────────────────────
  private renderTradeMarkers(): void {
    if (!this.candleSeries) return;
    const tradeList = this.trades();
    if (!tradeList.length) return;

    const markers: any[] = [];

    for (const trade of tradeList) {
      const entryTs = this.parseTimestampToSeconds(trade.entry_timestamp);
      const exitTs = this.parseTimestampToSeconds(trade.exit_timestamp);

      // Entry marker
      markers.push({
        time: entryTs as UTCTimestamp,
        position: 'belowBar',
        color: '#2196f3',
        shape: 'arrowUp',
        text: `${trade.trade_type} Entry`,
      });

      // Exit marker
      markers.push({
        time: exitTs as UTCTimestamp,
        position: 'aboveBar',
        color: trade.pnl >= 0 ? '#4caf50' : '#f44336',
        shape: 'arrowDown',
        text: `Exit ${trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(2)}`,
      });
    }

    markers.sort((a, b) => (a.time as number) - (b.time as number));

    if (this.markersPlugin) {
      this.markersPlugin.setMarkers(markers);
    } else {
      this.markersPlugin = createSeriesMarkers(this.candleSeries, markers);
    }
  }

  private parseTimestampToSeconds(ts: string): number {
    // Handle "YYYY-MM-DD HH:MM" or ISO format
    const d = new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + ':00Z');
    return Math.floor(d.getTime() / 1000);
  }

  // ──────────────────────────────────────────────
  // Overlay indicators on main chart
  // ──────────────────────────────────────────────
  private renderOverlays(): void {
    if (!this.mainChart) return;

    for (const [, series] of this.overlaySeries) {
      this.mainChart.removeSeries(series);
    }
    this.overlaySeries.clear();

    const results = this.chartIndicators();
    const overlayResults = results.filter(r => r.panel === 'main');

    for (const ind of overlayResults) {
      if (ind.type === 'line' && Array.isArray(ind.data)) {
        const lineData: LineData[] = (ind.data as IndicatorPoint[])
          .filter(p => p.value !== null)
          .map(p => ({ time: (p.t / 1000) as UTCTimestamp, value: p.value! }))
          .sort((a, b) => (a.time as number) - (b.time as number));

        if (lineData.length === 0) continue;

        let color = ind.color;
        const emaMatch = ind.id.match(/^ema[_.](\d+)/i);
        if (emaMatch) {
          color = getEmaRibbonColor(parseInt(emaMatch[1], 10)) ?? ind.color;
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

    const results = this.chartIndicators();
    const hostEl = this.subPanelHost()?.nativeElement;
    if (!hostEl) return;

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
    const container = document.createElement('div');
    container.className = 'sub-panel';
    container.style.width = '100%';
    container.style.height = '150px';
    container.style.marginTop = '4px';
    container.style.position = 'relative';

    const label = document.createElement('div');
    label.className = 'sub-panel-label';
    label.textContent = panelId.toUpperCase();

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
      rightPriceScale: { borderColor: DARK.border },
    });

    chart.timeScale().applyOptions({ shiftVisibleRangeOnNewBar: false });

    const seriesMap = new Map<string, ISeriesApi<any>>();

    for (const ind of indicators) {
      if (ind.type === 'macd' && !Array.isArray(ind.data)) {
        const macdData = ind.data as Record<string, IndicatorPoint[]>;

        if (macdData['histogram']) {
          const histSeries = chart.addSeries(HistogramSeries, {
            priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
          });
          const sorted = macdData['histogram'].filter(p => p.value !== null).sort((a, b) => a.t - b.t);
          const histData: HistogramData[] = sorted.map((p, i) => {
            const val = p.value!;
            const prev = i > 0 ? sorted[i - 1].value! : 0;
            const isGrowing = Math.abs(val) >= Math.abs(prev);
            const opacity = isGrowing ? 0.85 : 0.35;
            const color = val >= 0
              ? `rgba(0, 200, 150, ${opacity})`
              : `rgba(229, 51, 78, ${opacity})`;
            return { time: (p.t / 1000) as UTCTimestamp, value: val, color };
          });
          histSeries.setData(histData);
          seriesMap.set(`${ind.id}_hist`, histSeries);
        }

        if (macdData['macd']) {
          const macdSeries = chart.addSeries(LineSeries, {
            color: '#3b82f6', lineWidth: 2,
            priceLineVisible: false, lastValueVisible: false,
          });
          const lineData: LineData[] = macdData['macd']
            .filter(p => p.value !== null)
            .map(p => ({ time: (p.t / 1000) as UTCTimestamp, value: p.value! }))
            .sort((a, b) => (a.time as number) - (b.time as number));
          macdSeries.setData(lineData);
          seriesMap.set(`${ind.id}_macd`, macdSeries);
        }

        if (macdData['signal']) {
          const signalSeries = chart.addSeries(LineSeries, {
            color: '#f59e0b', lineWidth: 1,
            priceLineVisible: false, lastValueVisible: false,
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
          color: ind.color, lineWidth: 2,
          priceLineVisible: false, lastValueVisible: false,
        });
        const lineData: LineData[] = (ind.data as IndicatorPoint[])
          .filter(p => p.value !== null)
          .map(p => ({ time: (p.t / 1000) as UTCTimestamp, value: p.value! }))
          .sort((a, b) => (a.time as number) - (b.time as number));
        lineSeries.setData(lineData);
        seriesMap.set(ind.id, lineSeries);

        if (ind.refs?.length) {
          for (const refVal of ind.refs) {
            lineSeries.createPriceLine({
              price: refVal,
              color: DARK.refLine,
              lineWidth: 1,
              lineStyle: 2,
              axisLabelVisible: true,
            });
          }
        }
      }
    }

    // Sync with main chart
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
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    this.applyVisibility(id);
  }

  toggleAllIndicators(visible: boolean): void {
    if (visible) {
      this.visibleIndicators.set(new Set(this.chartIndicators().map(i => i.id)));
    } else {
      this.visibleIndicators.set(new Set());
    }
    for (const ind of this.chartIndicators()) {
      this.applyVisibility(ind.id);
    }
  }

  private applyVisibility(id: string): void {
    const isVisible = this.visibleIndicators().has(id);
    const ind = this.chartIndicators().find(r => r.id === id);
    if (!ind) return;

    if (ind.panel === 'main') {
      const series = this.overlaySeries.get(id);
      if (series) series.applyOptions({ visible: isVisible });
    } else {
      this.syncSubPanelVisibility(ind.panel);
    }
  }

  private syncSubPanelVisibility(panelId: string): void {
    const visible = this.visibleIndicators();
    const panelIndicators = this.chartIndicators().filter(r => r.panel === panelId);
    const anyVisible = panelIndicators.some(r => visible.has(r.id));
    const existingPanel = this.subPanels.find(p => p.id === panelId);

    if (anyVisible && !existingPanel) {
      const hostEl = this.subPanelHost()?.nativeElement;
      if (hostEl) {
        this.createSubPanel(hostEl, panelId, panelIndicators.filter(r => visible.has(r.id)));
        if (this.mainChart) {
          const mainRange = this.mainChart.timeScale().getVisibleLogicalRange();
          const newPanel = this.subPanels.find(p => p.id === panelId);
          if (mainRange && newPanel) {
            newPanel.chart.timeScale().setVisibleLogicalRange(mainRange);
          }
        }
      }
    } else if (!anyVisible && existingPanel) {
      this.removeSubPanel(panelId);
    } else if (anyVisible && existingPanel) {
      for (const ind of panelIndicators) {
        const isVis = visible.has(ind.id);
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
  // Quality report
  // ──────────────────────────────────────────────
  toggleQuality(): void {
    this.qualityModalOpen.update(v => !v);
  }
}
