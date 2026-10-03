import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';

import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import { DualPaneChartComponent, type ChartPane } from '../dual-pane-chart/dual-pane-chart.component';
import { toCandle } from '../lib/chart-bar-mapping';
import type { ChartLane } from '../strategy-view/chart-lanes';
import { historyUnavailableNotice } from '../lib/chart-history-notice';
import type {
  ChartHistoryResponse,
  ChartHistoryTimeframe,
  ChartLiveResolution,
  ChartLiveResponse,
} from '../lib/broker-v2-panel.types';

/**
 * The bot's day on the market tape: IBKR live bars with the bot's fills, and
 * Polygon's delayed history as a second pane.
 *
 * The tape's header price is the close of the last IBKR bar the live pane
 * holds, labelled as such (hurdle H15: IBKR supplies every price the bot page
 * shows). It owns no loading; the page shell drives every read.
 */
@Component({
  selector: 'app-bot-day-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DualPaneChartComponent],
  templateUrl: './bot-day-chart.component.html',
  styleUrl: './bot-day-chart.component.scss',
})
export class BotDayChartComponent {
  readonly symbol = input.required<string>();
  readonly liveChart = input<ChartLiveResponse | null>(null);
  readonly histChart = input<ChartHistoryResponse | null>(null);
  readonly liveChartLoading = input(false);
  readonly histChartLoading = input(false);
  /** Settled-error state for the delayed pane (#2202 FR-002). */
  readonly histChartFailed = input(false);
  readonly liveResolution = input<ChartLiveResolution>('5s');
  readonly historyTimeframe = input<ChartHistoryTimeframe>('1m');
  /** The pane it opens on: live for a running bot, the delayed tape (a finished run's window) otherwise. */
  readonly initialPane = input<ChartPane>('live');
  /** The run's lanes, drawn under the tape on its clock (#2808). */
  readonly lanes = input<readonly ChartLane[]>([]);

  readonly historyTimeframeChange = output<ChartHistoryTimeframe>();
  readonly liveResolutionChange = output<ChartLiveResolution>();
  /** One explicit retry of the delayed history (#2202 FR-005/FR-006). */
  readonly historyRetry = output();

  protected readonly liveBars = computed(() => this.liveChart()?.bars ?? []);
  protected readonly liveFillMarkers = computed(() => this.liveChart()?.fill_markers ?? []);
  protected readonly liveNotices = computed(() => this.liveChart()?.overlay_notices ?? []);
  protected readonly liveFeed = computed(() => this.liveChart()?.feed ?? null);
  protected readonly histBars = computed(() => this.histChart()?.bars ?? []);
  protected readonly histIndicatorBars = computed(() => this.histChart()?.indicator_bars ?? []);
  protected readonly histIndicatorBarBudget = computed(() => this.histChart()?.indicator_bar_budget ?? 0);
  protected readonly histIndicatorBarBudgetSatisfied = computed(
    () => this.histChart()?.indicator_bar_budget_satisfied ?? true,
  );
  protected readonly histFillMarkers = computed(() => this.histChart()?.fill_markers ?? []);
  /** A settled zero-bar history that is unavailable rather than empty (#2211 FR-002). */
  protected readonly histChartUnavailableNotice = computed(() => historyUnavailableNotice(this.histChart()));

  /** The last IBKR bar's close, or no price until the live pane holds a bar. */
  protected readonly tickerQuote = computed<TickerQuoteView | null>(() => {
    const last = this.liveBars().at(-1);
    return last === undefined
      ? null
      : { ticker: this.symbol(), price: toCandle(last).close, changePercent: null };
  });
}
