import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import type {
  BotPanelView,
  ChartHistoryPreset,
  ChartLiveResolution,
  ChartLiveResponse,
  PanelActionTrigger,
  ChartHistoryResponse,
  PanelProfile,
} from '../lib/broker-v2-panel.types';
import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import { DualPaneChartComponent } from '../dual-pane-chart/dual-pane-chart.component';
import { TradesTodayListComponent } from './trades-today-list.component';
import { TraderMetricsComponent } from './trader-metrics.component';
import { TraderBotBannerComponent } from './trader-bot-banner/trader-bot-banner.component';

/**
 * Trader lens (spec §6).
 *
 * Renders strictly from the panel projection and chart data received from the
 * shell. Owns no data loading — the shell drives all fetches and passes data
 * down as inputs. This keeps the lens replaceable (S4 operator lens uses the
 * same shell, different lens component).
 *
 * Log-only degradation: when `panel.mode === 'log_only'`, the trades/P&L
 * region is replaced by an honest observation-only panel. The spec requires
 * no empty tables.
 */
@Component({
  selector: 'app-trader-lens',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DualPaneChartComponent,
    TradesTodayListComponent,
    TraderBotBannerComponent,
    TraderMetricsComponent,
  ],
  templateUrl: './trader-lens.component.html',
  styleUrl: './trader-lens.component.scss',
})
export class TraderLensComponent {
  // ── Inputs ────────────────────────────────────────────────────────────────

  readonly panel = input.required<BotPanelView>();
  readonly tickerQuote = input<TickerQuoteView | null>(null);
  readonly profile = input.required<PanelProfile>();
  readonly liveChart = input<ChartLiveResponse | null>(null);
  readonly histChart = input<ChartHistoryResponse | null>(null);
  readonly liveChartLoading = input(false);
  readonly histChartLoading = input(false);
  readonly liveResolution = input<ChartLiveResolution>('5s');
  readonly selectedPreset = input<ChartHistoryPreset>('1D');
  readonly actionPending = input(false);

  // ── Outputs ───────────────────────────────────────────────────────────────

  /** User selected a history preset. */
  readonly presetChange = output<ChartHistoryPreset>();
  readonly liveResolutionChange = output<ChartLiveResolution>();
  readonly actionRequested = output<PanelActionTrigger>();

  // ── Derived ───────────────────────────────────────────────────────────────

  protected readonly isLogOnly = computed(() => this.panel().mode === 'log_only');
  protected readonly isPaperExecution = computed(() => this.panel().mode === 'trade');

  protected readonly symbol = computed(() => this.panel().symbol);

  protected readonly liveBars = computed(() => this.liveChart()?.bars ?? []);
  protected readonly liveFillMarkers = computed(
    () => this.liveChart()?.fill_markers ?? [],
  );
  protected readonly liveNotices = computed(
    () => this.liveChart()?.overlay_notices ?? [],
  );
  protected readonly histBars = computed(() => this.histChart()?.bars ?? []);
  protected readonly histFillMarkers = computed(
    () => this.histChart()?.fill_markers ?? [],
  );

  /** Today's trading date in ms UTC for the trades-today header. */
  protected readonly tradingDateMs = computed(
    () => this.liveChart()?.trading_date_open_ms ?? null,
  );

  // ── Template handlers ─────────────────────────────────────────────────────

  protected onPresetChange(preset: ChartHistoryPreset): void {
    this.presetChange.emit(preset);
  }
}
