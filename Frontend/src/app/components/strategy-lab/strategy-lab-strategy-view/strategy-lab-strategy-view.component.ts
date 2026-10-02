import { HttpClient, HttpErrorResponse } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, inject, input } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { forkJoin, from, map, of, switchMap, throwError } from "rxjs";

import { environment } from "../../../../environments/environment";
import type { components } from "../../../api/broker.types";
import type { BacktestRunDetail } from "../../../services/backtest-runs.types";
import { runWindowDate } from "../../../services/backtest-runs.types";
import { LeanSidecarService } from "../../../services/lean-sidecar.service";
import { refusalBody } from "../../../shared/errors/refusal-body";
import type { TradingMarker, TradingPoint } from "../../../shared/trading-chart";
import type { StrategyViewResponse } from "../../broker/v2-panel/lib/broker-v2-panel.types";
import { BotChartPanelComponent } from "../../broker/v2-panel/strategy-view/bot-chart-panel.component";
import type { StrategyViewFailure } from "../../broker/v2-panel/strategy-view/strategy-view-model";
import { StrategyLabChartComponent } from "../strategy-lab-chart/strategy-lab-chart.component";
import { parseStrategyParameters, previousIsoDate } from "../strategy-lab.models";

type EngineStrategyViewRequest = components["schemas"]["EngineStrategyViewRequest"];

/** What to ask for: the run's settings and data, with its window and warmup as trading dates. */
type StrategyViewInput =
  | {
      readonly kind: "request";
      readonly request: Omit<EngineStrategyViewRequest, "from_ms_utc" | "to_ms_utc" | "warmup_from_ms_utc">;
      readonly fromDate: string;
      readonly toDate: string;
      readonly warmupFromDate: string | null;
    }
  | { readonly kind: "unavailable"; readonly failure: StrategyViewFailure };

/**
 * A backtest's strategy view (#2639 D13): the bot page's chart panel on the
 * run's own decision candles, lines, default gate and saved gates, so a
 * backtest reads exactly like the live bot. The run's price-and-trades chart
 * is the panel's second tab.
 *
 * The view is the run replayed: the same registered program, settings, bars
 * and warmup, read from `POST /api/engine/strategy-view`. Its window and
 * warmup are session opens, resolved as the run's price chart resolves them.
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
  private readonly leanSidecar = inject(LeanSidecarService);

  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly equityPoints = input<readonly TradingPoint[]>([]);

  private readonly viewInput = computed((): StrategyViewInput => {
    const run = this.run();
    const policy = run.dataPolicy;
    if (!policy) {
      return {
        kind: "unavailable",
        failure: {
          message: "This run’s strategy view is unavailable.",
          why: "The run recorded no data policy, so its bars cannot be replayed.",
        },
      };
    }
    let parameters: ReturnType<typeof parseStrategyParameters>;
    try {
      parameters = parseStrategyParameters(run.parameters);
    } catch (error) {
      return {
        kind: "unavailable",
        failure: {
          message: "This run’s strategy view is unavailable.",
          why: error instanceof Error ? error.message : "The run’s saved settings are malformed.",
        },
      };
    }
    return {
      kind: "request",
      request: {
        strategy_name: run.strategyName,
        parameters,
        symbol: policy.symbol,
        adjusted: policy.adjusted,
        session: policy.session,
      },
      fromDate: runWindowDate(run.startDate),
      toDate: runWindowDate(run.endDate),
      warmupFromDate: run.warmupFromDate === null ? null : runWindowDate(run.warmupFromDate),
    };
  });

  protected readonly viewRead = rxResource<StrategyViewResponse, StrategyViewInput>({
    params: () => this.viewInput(),
    stream: ({ params }) => {
      if (params.kind === "unavailable") return throwError(() => new StrategyViewUnavailable(params.failure));
      return forkJoin({
        start: from(this.leanSidecar.nextTradingDayOpen(previousIsoDate(params.fromDate))),
        end: from(this.leanSidecar.nextTradingDayOpen(params.toDate)),
        warmup: params.warmupFromDate === null
          ? of(null)
          : from(this.leanSidecar.nextTradingDayOpen(previousIsoDate(params.warmupFromDate))),
      }).pipe(
        map(({ start, end, warmup }): EngineStrategyViewRequest => ({
          ...params.request,
          from_ms_utc: start.session_open_ms_utc,
          to_ms_utc: end.session_open_ms_utc,
          warmup_from_ms_utc: warmup?.session_open_ms_utc ?? null,
        })),
        switchMap((request) =>
          this.http.post<StrategyViewResponse>(`${environment.pythonServiceUrl}/api/engine/strategy-view`, request),
        ),
      );
    },
  });

  protected readonly view = computed(() => (this.viewRead.hasValue() ? this.viewRead.value() : null));

  /** A read that failed, in the backend's words when it gave them. */
  protected readonly failure = computed((): StrategyViewFailure | null => {
    const error = this.viewRead.error();
    if (error === undefined) return null;
    if (error instanceof StrategyViewUnavailable) return error.failure;
    return { message: "This run’s strategy view could not be read.", why: backendReason(error) };
  });
}

/** The run cannot be replayed at all; carried as the read's error so it renders like one. */
class StrategyViewUnavailable extends Error {
  constructor(readonly failure: StrategyViewFailure) {
    super(failure.message);
  }
}

/** The engine's reason: a 400 carries it as a plain `detail` string. */
function backendReason(error: unknown): string | null {
  if (!(error instanceof HttpErrorResponse)) return null;
  const body: unknown = error.error;
  if (typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string") {
    return body.detail;
  }
  const refusal = refusalBody(error)?.["message"];
  return typeof refusal === "string" ? refusal : null;
}
