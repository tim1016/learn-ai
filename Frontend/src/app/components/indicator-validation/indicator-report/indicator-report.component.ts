import {
  Component, signal, computed, inject, effect, viewChildren,
  ElementRef, DestroyRef, ChangeDetectionStrategy, afterNextRender,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import {
  createChart, IChartApi,
  CandlestickSeries, LineSeries, HistogramSeries,
  CandlestickData, LineData, HistogramData, UTCTimestamp,
} from 'lightweight-charts';
import { MarketDataService } from '../../../services/market-data.service';
import { IndicatorTableRow } from '../../../graphql/types';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';

interface CsvRow {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  bb_basis: number | null;
  bb_upper: number | null;
  bb_lower: number | null;
  supertrend_up: number | null;
  supertrend_down: number | null;
  ema_5: number | null;
  ema_10: number | null;
  ema_20: number | null;
  ema_30: number | null;
  ema_40: number | null;
  ema_50: number | null;
  ema_100: number | null;
  ema_200: number | null;
  rsi: number | null;
  rsi_ma: number | null;
  macd: number | null;
  macd_signal: number | null;
  macd_histogram: number | null;
  adx: number | null;
  [key: string]: string | number | null;
}

interface ChartPanel {
  id: string;
  title: string;
  key: string;
  type: 'price' | 'line' | 'histogram';
}

const PANDAS_TA_COLOR = '#26a69a';
const TRADINGVIEW_COLOR = '#2962FF';

const CHART_PANELS: ChartPanel[] = [
  { id: 'price', title: 'OHLCV — Candlestick + Close', key: 'close', type: 'price' },
  { id: 'ema_5', title: 'EMA 5', key: 'ema_5', type: 'line' },
  { id: 'ema_10', title: 'EMA 10', key: 'ema_10', type: 'line' },
  { id: 'ema_20', title: 'EMA 20', key: 'ema_20', type: 'line' },
  { id: 'ema_30', title: 'EMA 30', key: 'ema_30', type: 'line' },
  { id: 'ema_50', title: 'EMA 50', key: 'ema_50', type: 'line' },
  { id: 'ema_100', title: 'EMA 100', key: 'ema_100', type: 'line' },
  { id: 'ema_200', title: 'EMA 200', key: 'ema_200', type: 'line' },
  { id: 'bb_basis', title: 'Bollinger Bands — Basis', key: 'bb_basis', type: 'line' },
  { id: 'bb_upper', title: 'Bollinger Bands — Upper', key: 'bb_upper', type: 'line' },
  { id: 'bb_lower', title: 'Bollinger Bands — Lower', key: 'bb_lower', type: 'line' },
  { id: 'supertrend_up', title: 'Supertrend Up', key: 'supertrend_up', type: 'line' },
  { id: 'supertrend_down', title: 'Supertrend Down', key: 'supertrend_down', type: 'line' },
  { id: 'rsi', title: 'RSI', key: 'rsi', type: 'line' },
  { id: 'rsi_ma', title: 'RSI MA', key: 'rsi_ma', type: 'line' },
  { id: 'macd', title: 'MACD', key: 'macd', type: 'line' },
  { id: 'macd_signal', title: 'MACD Signal', key: 'macd_signal', type: 'line' },
  { id: 'macd_histogram', title: 'MACD Histogram', key: 'macd_histogram', type: 'histogram' },
  { id: 'adx', title: 'ADX', key: 'adx', type: 'line' },
];

@Component({
  selector: 'app-indicator-report',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, PageHeaderComponent],
  templateUrl: './indicator-report.component.html',
  styleUrls: ['./indicator-report.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class IndicatorReportComponent {
  private destroyRef = inject(DestroyRef);
  private marketData = inject(MarketDataService);

  ticker = signal('SPY');
  fromDate = signal('2026-03-26');
  toDate = signal('2026-03-27');
  loading = signal(false);
  error = signal('');
  generated = signal(false);

  csvRows = signal<CsvRow[]>([]);
  ourRows = signal<IndicatorTableRow[]>([]);
  csvFileName = signal('');

  panels = CHART_PANELS;
  private charts = new Map<string, IChartApi>();
  private chartContainers = viewChildren<ElementRef<HTMLDivElement>>('chartContainer');
  private renderReady = signal(false);

  hasCsv = computed(() => this.csvRows().length > 0);
  hasOur = computed(() => this.ourRows().length > 0);

  fieldStats = computed(() => {
    const csv = this.csvRows();
    const ours = this.ourRows();
    if (!csv.length || !ours.length) return [];

    const allFields = [
      'close', 'bb_basis', 'bb_upper', 'bb_lower',
      'supertrend_up', 'supertrend_down',
      'ema_5', 'ema_10', 'ema_20', 'ema_30',
      'ema_40', 'ema_50', 'ema_100', 'ema_200',
      'rsi', 'rsi_ma', 'macd', 'macd_signal', 'macd_histogram', 'adx',
    ];

    return allFields.map(field => {
      let count = 0, sumAbsDiff = 0, maxAbsDiff = 0, sumPctDiff = 0;
      const len = Math.min(csv.length, ours.length);

      for (let i = 0; i < len; i++) {
        const csvVal = (csv[i] as Record<string, unknown>)[field] as number | null;
        const ourVal = (ours[i] as Record<string, unknown>)[field] as number | null;
        if (csvVal != null && ourVal != null) {
          const d = Math.abs(ourVal - csvVal);
          sumAbsDiff += d;
          maxAbsDiff = Math.max(maxAbsDiff, d);
          if (csvVal !== 0) sumPctDiff += Math.abs((ourVal - csvVal) / csvVal) * 100;
          count++;
        }
      }

      return {
        field,
        count,
        meanAbsDiff: count > 0 ? sumAbsDiff / count : 0,
        maxAbsDiff,
        meanPctDiff: count > 0 ? sumPctDiff / count : 0,
        grade: count === 0 ? 'N/A' :
          (sumPctDiff / count) < 0.001 ? 'Exact' :
          (sumPctDiff / count) < 0.01 ? 'Close' :
          (sumPctDiff / count) < 0.1 ? 'OK' : 'Divergent',
      };
    });
  });

  constructor() {
    afterNextRender(() => {
      this.renderReady.set(true);
    });

    effect(() => {
      if (!this.renderReady()) return;
      if (!this.generated()) return;
      const csv = this.csvRows();
      const ours = this.ourRows();
      if (!csv.length && !ours.length) return;

      requestAnimationFrame(() => this.buildAllCharts());
    });

    this.destroyRef.onDestroy(() => {
      this.charts.forEach(c => c.remove());
      this.charts.clear();
    });
  }

  onCsvUpload(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    this.csvFileName.set(file.name);
    const reader = new FileReader();
    reader.onload = () => this.parseTradingViewCsv(reader.result as string);
    reader.readAsText(file);
  }

  private parseTradingViewCsv(text: string): void {
    const lines = text.trim().split('\n');
    if (lines.length < 2) return;
    const rows: CsvRow[] = [];
    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(',');
      if (cols.length < 29) continue;
      const p = (val: string): number | null => {
        if (!val || val.trim() === '') return null;
        const n = parseFloat(val);
        return isNaN(n) ? null : n;
      };
      rows.push({
        time: cols[0], open: parseFloat(cols[1]), high: parseFloat(cols[2]),
        low: parseFloat(cols[3]), close: parseFloat(cols[4]),
        bb_basis: p(cols[5]), bb_upper: p(cols[6]), bb_lower: p(cols[7]),
        supertrend_up: p(cols[8]), supertrend_down: p(cols[9]),
        ema_5: p(cols[10]), ema_10: p(cols[11]), ema_20: p(cols[12]), ema_30: p(cols[13]),
        ema_40: p(cols[14]), ema_50: p(cols[15]), ema_100: p(cols[16]), ema_200: p(cols[17]),
        volume: parseFloat(cols[18]),
        rsi: p(cols[19]), rsi_ma: p(cols[20]),
        macd_histogram: p(cols[25]), macd: p(cols[26]), macd_signal: p(cols[27]),
        adx: p(cols[28]),
      });
    }
    this.csvRows.set(rows);
  }

  async generate(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    this.generated.set(false);
    try {
      const result = await firstValueFrom(
        this.marketData.generateIndicatorTable(this.ticker(), this.fromDate(), this.toDate())
      );
      if (!result.success) {
        this.error.set(result.error ?? 'Unknown error');
        return;
      }
      const parsed: IndicatorTableRow[] = result.rows.map(s => JSON.parse(s));
      this.ourRows.set(parsed);
      this.generated.set(true);
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loading.set(false);
    }
  }

  private buildAllCharts(): void {
    this.charts.forEach(c => c.remove());
    this.charts.clear();

    const containers = this.chartContainers();
    for (let idx = 0; idx < this.panels.length; idx++) {
      const panel = this.panels[idx];
      const el = containers[idx]?.nativeElement;
      if (!el) continue;

      const height = panel.type === 'price' ? 350 : 220;
      const chart = buildChart(el, height);
      this.charts.set(panel.id, chart);

      if (panel.type === 'price') {
        this.buildPriceChart(chart);
      } else if (panel.type === 'histogram') {
        this.buildHistogramChart(chart, panel.key);
      } else {
        this.buildComparisonChart(chart, panel.key);
      }

      const observer = new ResizeObserver(([entry]) => {
        chart.applyOptions({ width: entry.contentRect.width });
      });
      observer.observe(el);
      this.destroyRef.onDestroy(() => observer.disconnect());
    }
  }

  private buildPriceChart(chart: IChartApi): void {
    const ours = this.ourRows();
    const csv = this.csvRows();

    if (ours.length) {
      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: '#26a69a', downColor: '#ef5350',
        borderUpColor: '#26a69a', borderDownColor: '#ef5350',
        wickUpColor: '#26a69a', wickDownColor: '#ef5350',
        title: 'Polygon OHLCV',
      });
      candleSeries.setData(
        ours
          .filter(r => r.open != null)
          .map(r => ({
            time: toUtc(r.time),
            open: r.open!, high: r.high!, low: r.low!, close: r.close!,
          } as CandlestickData))
          .sort(byTime)
      );
    }

    if (csv.length) {
      const csvClose = chart.addSeries(LineSeries, {
        color: TRADINGVIEW_COLOR, lineWidth: 1, title: 'TradingView Close',
        lineStyle: 2,
      });
      csvClose.setData(
        csv
          .filter(r => r.close != null)
          .map(r => ({ time: parseCsvTime(r.time), value: r.close } as LineData))
          .sort(byTime)
      );
    }

    if (ours.length) {
      const volSeries = chart.addSeries(HistogramSeries, {
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
      });
      chart.priceScale('volume').applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
      });
      volSeries.setData(
        ours
          .filter(r => r.volume != null)
          .map(r => ({
            time: toUtc(r.time),
            value: r.volume!,
            color: (r.close ?? 0) >= (r.open ?? 0) ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)',
          } as HistogramData))
          .sort(byTime)
      );
    }

    chart.timeScale().fitContent();
  }

  private buildComparisonChart(chart: IChartApi, key: string): void {
    const ours = this.ourRows();
    const csv = this.csvRows();

    if (ours.length) {
      const series = chart.addSeries(LineSeries, {
        color: PANDAS_TA_COLOR,
        lineWidth: 2,
        title: 'pandas-ta',
      });
      series.setData(
        ours
          .filter(r => (r as Record<string, unknown>)[key] != null)
          .map(r => ({ time: toUtc(r.time), value: (r as Record<string, unknown>)[key] as number } as LineData))
          .sort(byTime)
      );
    }

    if (csv.length) {
      const series = chart.addSeries(LineSeries, {
        color: TRADINGVIEW_COLOR,
        lineWidth: 1,
        lineStyle: 2,
        title: 'TradingView',
      });
      series.setData(
        csv
          .filter(r => (r as Record<string, unknown>)[key] != null)
          .map(r => ({ time: parseCsvTime(r.time), value: (r as Record<string, unknown>)[key] as number } as LineData))
          .sort(byTime)
      );
    }

    chart.timeScale().fitContent();
  }

  private buildHistogramChart(chart: IChartApi, key: string): void {
    const ours = this.ourRows();
    const csv = this.csvRows();

    if (ours.length) {
      const series = chart.addSeries(HistogramSeries, { title: 'pandas-ta' });
      series.setData(
        ours
          .filter(r => (r as Record<string, unknown>)[key] != null)
          .map(r => {
            const v = (r as Record<string, unknown>)[key] as number;
            return { time: toUtc(r.time), value: v, color: v >= 0 ? 'rgba(38,166,154,0.7)' : 'rgba(239,83,80,0.7)' } as HistogramData;
          })
          .sort(byTime)
      );
    }

    if (csv.length) {
      const series = chart.addSeries(HistogramSeries, {
        title: 'TradingView',
        priceScaleId: 'tv_hist',
      });
      chart.priceScale('tv_hist').applyOptions({ scaleMargins: { top: 0, bottom: 0.5 } });
      series.setData(
        csv
          .filter(r => (r as Record<string, unknown>)[key] != null)
          .map(r => {
            const v = (r as Record<string, unknown>)[key] as number;
            return { time: parseCsvTime(r.time), value: v, color: v >= 0 ? 'rgba(41,98,255,0.5)' : 'rgba(233,30,99,0.5)' } as HistogramData;
          })
          .sort(byTime)
      );
    }

    chart.timeScale().fitContent();
  }

  gradeClass(grade: string): string {
    switch (grade) {
      case 'Exact': return 'grade-exact';
      case 'Close': return 'grade-close';
      case 'OK': return 'grade-ok';
      case 'Divergent': return 'grade-bad';
      default: return '';
    }
  }
}

// ── Pure helpers ──

function toUtc(timestamp: number): UTCTimestamp {
  return (timestamp / 1000) as UTCTimestamp;
}

function parseCsvTime(timeStr: string): UTCTimestamp {
  return (new Date(timeStr).getTime() / 1000) as UTCTimestamp;
}

function byTime(a: { time: unknown }, b: { time: unknown }): number {
  return (a.time as number) - (b.time as number);
}

function buildChart(container: HTMLElement, height: number): IChartApi {
  return createChart(container, {
    width: container.clientWidth,
    height,
    layout: { background: { color: '#1a1a2e' }, textColor: '#c8c8d0' },
    grid: { vertLines: { color: '#2a2a3e' }, horzLines: { color: '#2a2a3e' } },
    timeScale: { timeVisible: true, borderColor: '#3a3a4e' },
    crosshair: { mode: 0 },
    rightPriceScale: { borderColor: '#3a3a4e' },
  });
}
