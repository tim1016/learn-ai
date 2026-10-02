import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, inject, input } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";

import { environment } from "../../../../environments/environment";
import type { BacktestRunDetail } from "../../../services/backtest-runs.types";
import { refusalBody } from "../../../shared/errors/refusal-body";
import type { TradingMarker, TradingPoint } from "../../../shared/trading-chart";
import type { StrategyViewResponse } from "../../broker/v2-panel/lib/broker-v2-panel.types";
import { BotChartPanelComponent } from "../../broker/v2-panel/strategy-view/bot-chart-panel.component";
import type { StrategyViewFailure } from "../../broker/v2-panel/strategy-view/strategy-view-model";
import { StrategyLabChartComponent } from "../strategy-lab-chart/strategy-lab-chart.component";

/**
 * A backtest's strategy view (#2639 D13): the bot page's chart panel on the
 * run's own decision candles, lines, default gate and saved gates, so a
 * backtest reads exactly like the live bot. The run's price-and-trades chart
 * is the panel's second tab.
 *
 * The data plane replays the saved run exactly as it ran and shows it only
 * when the replay reproduces the run's trades
 * (`GET /api/research/backtest-runs/{id}/strategy-view`); when it cannot, the
 * panel says why in the data plane's words.
 */
@Component({
  selector: "app-strategy-lab-strategy-view",
  imports: [BotChartPanelComponent, StrategyLabChartComponent],
  template: `
    <app-bot-chart-panel
      [symbol]="run().symbol"
      [view]="view()"
      [loading]="viewRead.isLoading()"
      [failure]="failure()"
      [tape]="labChart"
      tapeLabel="Price & trades"
      (retry)="viewRead.reload()"
    />
    <ng-template #labChart>
      <app-strategy-lab-chart [run]="run()" [markers]="markers()" [equityPoints]="equityPoints()" />
    </ng-template>
  `,
  styles: `:host { display: block; min-width: 0; min-height: 0; height: 100%; }`,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabStrategyViewComponent {
  private readonly http = inject(HttpClient);

  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly equityPoints = input<readonly TradingPoint[]>([]);

  protected readonly viewRead = rxResource<StrategyViewResponse, number>({
    params: () => this.run().id,
    stream: ({ params }) =>
      this.http.get<StrategyViewResponse>(
        `${environment.pythonServiceUrl}/api/research/backtest-runs/${params}/strategy-view`,
      ),
  });

  protected readonly view = computed(() => (this.viewRead.hasValue() ? this.viewRead.value() : null));

  /** A read that failed, in the data plane's words when it gave them. */
  protected readonly failure = computed((): StrategyViewFailure | null => {
    const error = this.viewRead.error();
    if (error === undefined) return null;
    const reason = refusalBody(error)?.["message"];
    return {
      message: "This run’s strategy view could not be shown.",
      why: typeof reason === "string" ? reason : null,
    };
  });
}
