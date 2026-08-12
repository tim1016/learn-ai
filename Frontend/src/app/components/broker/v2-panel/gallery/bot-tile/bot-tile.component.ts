import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';
import {
  CandlestickSeries,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';
import type { ChartBar, ChartFillMarker, GalleryBotView } from '../lib/gallery.types';
import { toCandle } from '../../lib/chart-bar-mapping';
import { fmtCurrency, fmtInteger, fmtSignedCurrency, fmtSignedNumber } from '../../../format';

/**
 * Map a ChartBar to a volume histogram point, colored by the bar's own
 * direction. Tile-specific (the volume overlay is not part of
 * `DualPaneChartComponent`'s market tape), so this stays local rather than
 * moving into the shared `chart-bar-mapping` module — see `toCandle` there
 * for the mapping that *is* shared.
 */
export function toVolumeBar(bar: ChartBar): { time: UTCTimestamp; value: number; color: string } {
  return {
    time: Math.floor(bar.start_ms / 1000) as UTCTimestamp,
    value: bar.volume,
    color: Number(bar.close) >= Number(bar.open) ? '#26a69a' : '#ef5350',
  };
}

/** Map fills to candle-series markers: buys below the bar, sells above. */
export function toTileMarkers(markers: readonly ChartFillMarker[]): SeriesMarker<UTCTimestamp>[] {
  return markers
    .map((marker) => {
      const isBuy = marker.side === 'buy';
      return {
        time: Math.floor(marker.filled_at_ms / 1000) as UTCTimestamp,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#26a69a' : '#ef5350',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: `${marker.side.toUpperCase()} ${marker.quantity} @ ${marker.price}`,
      } satisfies SeriesMarker<UTCTimestamp>;
    })
    .sort((a, b) => a.time - b.time);
}

type PnlTone = 'positive' | 'negative' | 'neutral';

function toneOf(value: number): PnlTone {
  if (value === 0) return 'neutral';
  return value > 0 ? 'positive' : 'negative';
}

/**
 * One live bot's gallery tile: header identity/price, a thin candlestick +
 * volume chart with fill markers, footer P&L, and a single guarded quick
 * action. The chart is mounted and updated imperatively so a 20-tile wall
 * never routes tick-by-tick data through Angular's change detection — see
 * `dual-pane-chart.component.ts` for the same lightweight-charts v5 idioms.
 */
@Component({
  selector: 'app-bot-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
  templateUrl: './bot-tile.component.html',
  styleUrl: './bot-tile.component.scss',
})
export class BotTileComponent {
  readonly bot = input.required<GalleryBotView>();
  readonly bars = input.required<readonly ChartBar[]>();
  readonly markers = input<readonly ChartFillMarker[]>([]);
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  /** Set by the dock while this tile's confirmed quick action is in flight — disables the button and marks it `aria-busy` without restyling the rest of the tile. */
  readonly pending = input<boolean>(false);

  readonly action = output<{ sid: string; actionId: string }>();

  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly chartContainer =
    viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');
  private readonly confirmCancelButton =
    viewChild<ElementRef<HTMLButtonElement>>('confirmCancelButton');

  protected readonly confirmOpen = signal(false);

  protected readonly fmtSignedCurrency = fmtSignedCurrency;
  protected readonly fmtInteger = fmtInteger;

  private readonly lastBar = computed<ChartBar | null>(() => {
    const bars = this.bars();
    return bars.length ? bars[bars.length - 1] : null;
  });
  private readonly firstBar = computed<ChartBar | null>(() => {
    const bars = this.bars();
    return bars.length ? bars[0] : null;
  });
  private readonly lastPrice = computed<number | null>(() => {
    const bar = this.lastBar();
    return bar ? Number(bar.close) : null;
  });
  private readonly deltaPct = computed<number | null>(() => {
    const first = this.firstBar();
    const last = this.lastBar();
    if (!first || !last) return null;
    const openPrice = Number(first.open);
    if (openPrice === 0) return null;
    return (Number(last.close) - openPrice) / openPrice;
  });

  protected readonly formattedPrice = computed(() => fmtCurrency(this.lastPrice()));
  protected readonly formattedDeltaPct = computed<string | null>(() => {
    const delta = this.deltaPct();
    return delta === null ? null : `${fmtSignedNumber(delta * 100, 2)}%`;
  });
  protected readonly deltaTone = computed<PnlTone>(() => {
    const delta = this.deltaPct();
    if (delta === null || delta === 0) return 'neutral';
    return delta > 0 ? 'positive' : 'negative';
  });

  protected readonly realizedTone = computed(() => toneOf(this.bot().realized_pnl_today));
  protected readonly openTone = computed(() => toneOf(this.bot().open_pnl));

  protected readonly actionTitle = computed<string | null>(() => {
    const primaryAction = this.bot().primary_action;
    return !primaryAction.enabled && primaryAction.disabled_reason
      ? primaryAction.disabled_reason
      : null;
  });
  protected readonly actionAriaLabel = computed<string | null>(() => {
    const primaryAction = this.bot().primary_action;
    return !primaryAction.enabled && primaryAction.disabled_reason
      ? `${primaryAction.label} — ${primaryAction.disabled_reason}`
      : null;
  });
  protected readonly confirmText = computed(() => {
    const view = this.bot();
    return `${view.primary_action.label} ${view.symbol} · ${view.sid}?`;
  });

  private chart: IChartApi | null = null;
  private candleSeries: ISeriesApi<'Candlestick'> | null = null;
  private volumeSeries: ISeriesApi<'Histogram'> | null = null;
  private markersApi: ISeriesMarkersPluginApi<Time> | null = null;

  constructor() {
    effect(() => this.syncChart());
    effect(() => {
      // Move keyboard focus into the confirm when it opens, mirroring
      // `TypedHaltConfirmComponent` — a wall of tiles with no focus
      // management would strand keyboard/screen-reader operators on the
      // toolbar behind the confirm for a live Stop/Resume control.
      if (this.confirmOpen()) {
        queueMicrotask(() => this.confirmCancelButton()?.nativeElement.focus());
      }
    });
    afterNextRender(() => this.mountChart());
  }

  protected onBodyClick(): void {
    void this.router.navigate([
      '/brokers', this.broker(), 'accounts', this.accountId(), 'bots', this.bot().sid,
    ]);
  }

  protected onBodySpaceKey(event: Event): void {
    // Space defaults to page-scroll on a focusable div; suppress that
    // since Space here activates navigation instead.
    event.preventDefault();
    this.onBodyClick();
  }

  protected onActionClick(): void {
    if (!this.bot().primary_action.enabled) return;
    this.confirmOpen.set(true);
  }

  protected confirmAction(): void {
    const view = this.bot();
    this.confirmOpen.set(false);
    this.action.emit({ sid: view.sid, actionId: view.primary_action.action_id });
  }

  protected cancelAction(): void {
    this.confirmOpen.set(false);
  }

  protected onEscape(): void {
    if (this.confirmOpen()) {
      this.cancelAction();
    }
  }

  private mountChart(): void {
    const container = this.chartContainer().nativeElement;
    this.chart = createChart(container, {
      layout: {
        background: { color: 'transparent' },
        textColor: '#9598a1',
      },
      grid: {
        vertLines: { color: 'rgba(42, 46, 57, 0.35)' },
        horzLines: { color: 'rgba(42, 46, 57, 0.35)' },
      },
      rightPriceScale: { borderColor: '#2a2e39' },
      timeScale: { borderColor: '#2a2e39', timeVisible: true },
      autoSize: true,
    });
    this.candleSeries = this.chart.addSeries(CandlestickSeries, {});
    this.volumeSeries = this.chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });
    this.volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    this.markersApi = createSeriesMarkers(this.candleSeries, []);
    this.syncChart();
    this.destroyRef.onDestroy(() => this.cleanup());
  }

  private syncChart(): void {
    const bars = this.bars();
    const markers = this.markers();
    if (!this.candleSeries || !this.volumeSeries || !this.markersApi) return;
    this.candleSeries.setData(bars.map(toCandle));
    this.volumeSeries.setData(bars.map(toVolumeBar));
    this.markersApi.setMarkers(toTileMarkers(markers));
  }

  private cleanup(): void {
    this.chart?.remove();
    this.chart = null;
    this.candleSeries = null;
    this.volumeSeries = null;
    this.markersApi = null;
  }
}
