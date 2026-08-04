import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { EMPTY_RUN_HISTORY_STATE } from '../lib/broker-v2-panel.types';
import type {
  BotPanelView,
  ChartHistoryPreset,
  ChartLiveResolution,
  ChartLiveResponse,
  ChartHistoryResponse,
  PanelAction,
  PanelActionTrigger,
  PanelProfile,
  RunHistoryNavigation,
  RunHistoryState,
} from '../lib/broker-v2-panel.types';
import { DualPaneChartComponent } from '../dual-pane-chart/dual-pane-chart.component';
import { TradesTodayListComponent } from './trades-today-list.component';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TraderMetricsComponent } from './trader-metrics.component';
import { BotRunHistoryComponent } from '../bot-run-history/bot-run-history.component';

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
    TimestampDisplayComponent,
    PanelActionButtonComponent,
    ReceiptLabelPipe,
    TraderMetricsComponent,
    BotRunHistoryComponent,
    RouterLink,
  ],
  templateUrl: './trader-lens.component.html',
  styleUrl: './trader-lens.component.scss',
})
export class TraderLensComponent {
  // ── Inputs ────────────────────────────────────────────────────────────────

  readonly panel = input.required<BotPanelView>();
  readonly profile = input.required<PanelProfile>();
  readonly liveChart = input<ChartLiveResponse | null>(null);
  readonly histChart = input<ChartHistoryResponse | null>(null);
  readonly liveChartLoading = input(false);
  readonly histChartLoading = input(false);
  readonly liveResolution = input<ChartLiveResolution>('5s');
  readonly selectedPreset = input<ChartHistoryPreset>('1D');
  readonly actionPending = input(false);
  readonly runHistory = input<RunHistoryState>(EMPTY_RUN_HISTORY_STATE);

  // ── Outputs ───────────────────────────────────────────────────────────────

  /** User selected a history preset. */
  readonly presetChange = output<ChartHistoryPreset>();
  readonly liveResolutionChange = output<ChartLiveResolution>();
  /** User clicked the primary verb button (Resume/Stop). */
  readonly actionRequested = output<PanelActionTrigger>();
  readonly runHistoryNavigation = output<RunHistoryNavigation>();

  // ── Derived ───────────────────────────────────────────────────────────────

  protected readonly isLogOnly = computed(() => this.panel().mode === 'log_only');
  protected readonly isDryRun = computed(() => this.panel().mode === 'dry_run');
  protected readonly isPaperExecution = computed(() => this.panel().mode === 'trade');

  protected readonly symbol = computed(() => this.panel().symbol);

  /**
   * Headline sourced from the latest decision receipt on the health card.
   * The backend provides the full sentence; Angular renders it verbatim.
   * Falls back to the desired-state label when no decision is recorded yet.
   */
  protected readonly headline = computed(() => {
    const health = this.panel().health;
    if (health.duty_outcome?.explanation) return health.duty_outcome.explanation;
    return health.desired_state_label;
  });

  /** The one primary verb action (Resume or Stop) — exactly one at a time. */
  protected readonly primaryAction = computed<PanelAction | null>(() => {
    const health = this.panel().health;
    const actionId = !health.running
      ? 'resume'
      : health.desired_state === 'PAUSED'
        ? 'continue'
        : 'stop';
    return this.panel().actions.find((action) => action.action_id === actionId) ?? null;
  });

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
