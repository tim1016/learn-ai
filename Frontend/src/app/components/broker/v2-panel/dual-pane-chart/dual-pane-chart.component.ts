import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import {
  CandlestickSeries,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
  createChart,
} from 'lightweight-charts';
import type { ChartBar, ChartFillMarker, ChartHistoryPreset, ChartSource } from '../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

/** Map a ChartBar to lightweight-charts candle data (seconds UTC). */
function toCandle(bar: ChartBar): {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
} {
  return {
    time: Math.floor(bar.start_ms / 1000) as UTCTimestamp,
    open: parseFloat(bar.open),
    high: parseFloat(bar.high),
    low: parseFloat(bar.low),
    close: parseFloat(bar.close),
  };
}

function sourceColors(source: ChartSource): {
  upColor: string;
  downColor: string;
  borderUpColor: string;
  borderDownColor: string;
  wickUpColor: string;
  wickDownColor: string;
} {
  if (source === 'polygon') {
    // Greyed inactive style for Polygon-only bars (fallback visual).
    return {
      upColor: '#9ca3af',
      downColor: '#9ca3af',
      borderUpColor: '#6b7280',
      borderDownColor: '#6b7280',
      wickUpColor: '#6b7280',
      wickDownColor: '#6b7280',
    };
  }
  return {
    upColor: '#22c55e',
    downColor: '#ef4444',
    borderUpColor: '#16a34a',
    borderDownColor: '#dc2626',
    wickUpColor: '#16a34a',
    wickDownColor: '#dc2626',
  };
}

export const HISTORY_PRESETS: readonly ChartHistoryPreset[] = [
  '1D', '5D', '1M', '3M', '1Y', 'All',
];

/**
 * Dual-pane market chart (spec §8).
 *
 * Two independent `lightweight-charts` instances rendered side-by-side on
 * desktop and stacked on narrow viewports. Each pane has per-pane full-screen
 * expand. LIVE pane uses today's source-tagged bars; HISTORY pane uses
 * bounded presets (1D·5D·1M·3M·1Y·All) with life-of-bot fills.
 *
 * Source provenance is truthfully tagged: `ibkr`=standard colours,
 * `polygon`=greyed inactive style. When the LIVE pane has no IBKR bars, an
 * overlay notice chip renders (from `overlay_notices` on the response) —
 * Angular never fabricates the message.
 *
 * Chart series update reactively: `effect()` calls track the signal inputs and
 * call the render helpers on any change. The chart instances are created once
 * in `ngAfterViewInit`; the effects are no-ops before that (guarded by null
 * series check inside each render helper).
 */
@Component({
  selector: 'app-dual-pane-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './dual-pane-chart.component.html',
  styleUrl: './dual-pane-chart.component.scss',
})
export class DualPaneChartComponent implements AfterViewInit {
  // ── Inputs ────────────────────────────────────────────────────────────────

  readonly symbol = input.required<string>();
  readonly liveBars = input<readonly ChartBar[]>([]);
  readonly liveFillMarkers = input<readonly ChartFillMarker[]>([]);
  readonly liveNotices = input<
    readonly { code: string; message: string }[]
  >([]);
  readonly histBars = input<readonly ChartBar[]>([]);
  readonly histFillMarkers = input<readonly ChartFillMarker[]>([]);
  readonly selectedPreset = input<ChartHistoryPreset>('1D');

  // ── Output ────────────────────────────────────────────────────────────────

  readonly presetChange = output<ChartHistoryPreset>();

  // ── View children ─────────────────────────────────────────────────────────

  private readonly liveContainer =
    viewChild.required<ElementRef<HTMLDivElement>>('liveContainer');
  private readonly histContainer =
    viewChild.required<ElementRef<HTMLDivElement>>('histContainer');

  // ── Internal state ────────────────────────────────────────────────────────

  protected readonly liveFullscreen = signal(false);
  protected readonly histFullscreen = signal(false);

  protected readonly liveSource = computed<ChartSource | null>(() => {
    const bars = this.liveBars();
    if (!bars.length) return null;
    const sources = new Set(bars.map((b) => b.source));
    if (sources.size === 1) return bars[0].source;
    return 'mixed';
  });

  protected readonly presets = HISTORY_PRESETS;

  private liveChart: IChartApi | null = null;
  private histChart: IChartApi | null = null;
  private liveSeries: ISeriesApi<'Candlestick'> | null = null;
  private histSeries: ISeriesApi<'Candlestick'> | null = null;

  private readonly destroyRef = inject(DestroyRef);

  // ── Constructor ───────────────────────────────────────────────────────────

  constructor() {
    // Re-render live pane whenever live bars or fill markers change.
    // Guard inside renderLive() is a no-op before ngAfterViewInit.
    effect(() => {
      this.liveFillMarkers(); // track for future marker rendering
      this.renderLive();
    });
    // Re-render history pane whenever history bars or fill markers change.
    effect(() => {
      this.histFillMarkers(); // track for future marker rendering
      this.renderHistory();
    });
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  ngAfterViewInit(): void {
    this.liveChart = createChart(
      this.liveContainer().nativeElement,
      this.chartOptions(),
    );
    this.liveSeries = this.liveChart.addSeries(CandlestickSeries, {});

    this.histChart = createChart(
      this.histContainer().nativeElement,
      this.chartOptions(),
    );
    this.histSeries = this.histChart.addSeries(CandlestickSeries, {});

    this.renderLive();
    this.renderHistory();

    this.destroyRef.onDestroy(() => this.cleanup());
  }

  // ── Template handlers ─────────────────────────────────────────────────────

  protected selectPreset(preset: ChartHistoryPreset): void {
    this.presetChange.emit(preset);
  }

  protected toggleLiveFullscreen(): void {
    this.liveFullscreen.update((v) => !v);
    requestAnimationFrame(() => this.liveChart?.timeScale().fitContent());
  }

  protected toggleHistFullscreen(): void {
    this.histFullscreen.update((v) => !v);
    requestAnimationFrame(() => this.histChart?.timeScale().fitContent());
  }

  // ── Private helpers ───────────────────────────────────────────────────────

  private chartOptions() {
    return {
      layout: {
        background: { color: 'transparent' },
        textColor: '#d1d5db',
      },
      grid: {
        vertLines: { color: '#374151' },
        horzLines: { color: '#374151' },
      },
      autoSize: true,
    };
  }

  private renderLive(): void {
    if (!this.liveSeries) return;
    const bars = this.liveBars();
    if (!bars.length) {
      this.liveSeries.setData([]);
      return;
    }
    const dominant = this.liveSource() ?? 'ibkr';
    this.liveSeries.applyOptions(sourceColors(dominant));
    this.liveSeries.setData(bars.map(toCandle));
    this.liveChart?.timeScale().fitContent();
  }

  private renderHistory(): void {
    if (!this.histSeries) return;
    const bars = this.histBars();
    if (!bars.length) {
      this.histSeries.setData([]);
      return;
    }
    // History pane always uses Polygon bars; match greyed style.
    this.histSeries.applyOptions(sourceColors('polygon'));
    this.histSeries.setData(bars.map(toCandle));
    this.histChart?.timeScale().fitContent();
  }

  private cleanup(): void {
    this.liveChart?.remove();
    this.histChart?.remove();
    this.liveChart = null;
    this.histChart = null;
    this.liveSeries = null;
    this.histSeries = null;
  }
}
