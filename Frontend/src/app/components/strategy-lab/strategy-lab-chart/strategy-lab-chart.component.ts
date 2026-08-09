import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { catchError, forkJoin, from, map, of, switchMap } from "rxjs";

import { environment } from "../../../../environments/environment";
import type { components } from "../../../api/broker.types";
import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { MarketDataService } from "../../../services/market-data.service";
import { LeanSidecarService } from "../../../services/lean-sidecar.service";
import { IndicatorCatalogService } from "../../../shared/indicator-catalog/indicator-catalog.service";
import type { IndicatorPickerAdd } from "../../../shared/indicator-picker/indicator-picker.component";
import {
  TradingChartComponent,
  type ChartIndicatorEntry,
  type TradingCandle,
  type TradingIndicatorChip,
  type TradingMarker,
  type TradingPoint,
  type TradingSeries,
  type TradingSubPane,
} from "../../../shared/trading-chart";
import {
  SERIES_COLORS,
  indicatorMatches,
  normalizeIndicatorResults,
  timeframeKey,
} from "./strategy-lab-chart.adapter";
import { parseStrategyParameters, previousIsoDate } from "../strategy-lab.models";

type EngineChartResponse = components["schemas"]["EngineChartResponse"];
type EngineChartRequest = components["schemas"]["EngineChartRequest"];

interface EngineChartRequestInput {
  kind: "request";
  request: Omit<EngineChartRequest, "from_ms_utc" | "to_ms_utc">;
  fromDate: string;
  toDate: string;
}

interface EngineChartUnavailableInput {
  kind: "unavailable";
  message: string;
}

type EngineChartInput = EngineChartRequestInput | EngineChartUnavailableInput;

type ChartState =
  | { kind: "loaded"; response: EngineChartResponse }
  | { kind: "unavailable"; message: string };

@Component({
  selector: "app-strategy-lab-chart",
  imports: [TradingChartComponent],
  templateUrl: "./strategy-lab-chart.component.html",
  styleUrl: "./strategy-lab-chart.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabChartComponent {
  private readonly http = inject(HttpClient);
  private readonly leanSidecar = inject(LeanSidecarService);
  protected readonly catalog = inject(IndicatorCatalogService);
  private readonly marketData = inject(MarketDataService);

  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly equityPoints = input<readonly TradingPoint[]>([]);

  private readonly userIndicators = signal<ChartIndicatorEntry[]>([]);
  private readonly excludedStrategyIndicatorIds = signal<string[]>([]);
  private seededRunId: number | null = null;

  constructor() {
    void this.catalog.load();
    effect(() => {
      const runId = this.run().id;
      if (runId === this.seededRunId) return;
      if (this.seededRunId === null) {
        this.seededRunId = runId;
        return;
      }
      this.seededRunId = runId;
      this.userIndicators.set([]);
      this.excludedStrategyIndicatorIds.set([]);
    });
  }

  private readonly chartInput = computed<EngineChartInput>(() => {
    const run = this.run();
    const policy = run.dataPolicy;
    const bars = policy?.strategy_bars;
    if (!policy || !bars || (bars.timespan !== "minute" && bars.timespan !== "hour" && bars.timespan !== "day")) {
      return {
        kind: "unavailable",
        message: "Price and signal evidence are unavailable because this run has no complete data policy.",
      };
    }
    let parameters: ReturnType<typeof parseStrategyParameters>;
    try {
      parameters = parseStrategyParameters(run.parameters);
    } catch (error) {
      return {
        kind: "unavailable",
        message: error instanceof Error
          ? `${error.message} Price and signal evidence cannot be reconstructed.`
          : "Saved run parameters are malformed; price and signal evidence cannot be reconstructed.",
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
        timespan: bars.timespan,
        multiplier: bars.multiplier,
        indicators: this.userIndicators(),
        excluded_strategy_indicator_ids: this.excludedStrategyIndicatorIds(),
      },
      fromDate: run.startDate,
      toDate: run.endDate,
    };
  });

  private readonly chartResource = rxResource<ChartState, EngineChartInput>({
    params: () => this.chartInput(),
    stream: ({ params }) => {
      if (params.kind === "unavailable") {
        return of<ChartState>({
          kind: "unavailable",
          message: params.message,
        });
      }
      return forkJoin({
        start: from(this.leanSidecar.nextTradingDayOpen(previousIsoDate(params.fromDate))),
        end: from(this.leanSidecar.nextTradingDayOpen(params.toDate)),
      }).pipe(
        switchMap(({ start, end }) => {
          const request: EngineChartRequest = {
            ...params.request,
            from_ms_utc: start.session_open_ms_utc,
            to_ms_utc: end.session_open_ms_utc,
          };
          return this.http.post<EngineChartResponse>(
            `${environment.pythonServiceUrl}/api/engine/chart`,
            request,
          );
        }),
        map((response): ChartState => ({ kind: "loaded", response })),
        catchError(() =>
          of<ChartState>({
            kind: "unavailable",
            message: "The exact run bar store is unavailable; price and signal evidence cannot be shown safely.",
          }),
        ),
      );
    },
  });

  private readonly quoteResource = rxResource({
    params: () => this.run().symbol,
    stream: ({ params }) =>
      this.marketData.getStockSnapshot(params).pipe(catchError(() => of(null))),
  });

  private readonly response = computed(() => {
    const state = this.chartResource.value();
    return state?.kind === "loaded" ? state.response : null;
  });

  readonly candles = computed<TradingCandle[]>(() =>
    (this.response()?.bars ?? []).map((bar) => ({
      timeMs: bar.t,
      open: bar.o,
      high: bar.h,
      low: bar.l,
      close: bar.c,
      volume: bar.v,
    })),
  );

  readonly indicatorNotice = computed(() => {
    const state = this.chartResource.value();
    if (state?.kind === "unavailable") return state.message;
    const coverage = state?.kind === "loaded" ? state.response.coverage : null;
    if (coverage && !coverage.is_complete) {
      return `Bar store covers ${coverage.available_days} of ${coverage.expected_days} sessions; missing sessions remain visible as gaps.`;
    }
    return null;
  });

  readonly quote = computed(() => {
    const run = this.run();
    const snapshot = this.quoteResource.value()?.snapshot;
    const candles = this.candles();
    const lastCandle = candles[candles.length - 1];
    const price = snapshot?.min?.close ?? snapshot?.day?.close ?? lastCandle?.close;
    if (price === null || price === undefined) return null;
    return {
      ticker: run.symbol,
      price,
      change: snapshot?.todaysChange ?? null,
      changePercent: snapshot?.todaysChangePercent ?? null,
    };
  });

  private readonly normalizedIndicators = computed(() =>
    normalizeIndicatorResults(this.response()?.indicators ?? []),
  );
  readonly overlays = computed<TradingSeries[]>(() => this.normalizedIndicators().overlays);
  readonly subPanes = computed<TradingSubPane[]>(() => this.normalizedIndicators().subPanes);
  readonly equity = computed<TradingSeries[]>(() =>
    this.equityPoints().length === 0
      ? []
      : [
          {
            id: "equity",
            name: "Realized equity",
            color: "#7aa9ff",
            type: "area",
            lineType: "steps",
            points: [...this.equityPoints()],
          },
        ],
  );
  readonly activeIndicators = computed<TradingIndicatorChip[]>(() =>
    (this.response()?.indicator_specs ?? []).map((spec, index) => ({
      id: spec.id,
      label: spec.label,
      color: this.response()?.indicators[index]?.color ?? SERIES_COLORS[index % SERIES_COLORS.length],
    })),
  );
  readonly timeframeLabel = computed(() => {
    const bars = this.run().dataPolicy?.strategy_bars;
    return bars ? timeframeKey(bars.timespan, bars.multiplier) : "1m";
  });

  addIndicator(event: IndicatorPickerAdd): void {
    const alreadyActive = this.response()?.indicator_specs.some((spec) =>
      indicatorMatches(spec.name, spec.params, event.name, event.params)) ?? false;
    if (alreadyActive) return;
    this.userIndicators.update((items) => [
      ...items,
      { name: event.name, params: { ...event.params } },
    ]);
  }

  removeIndicator(id: string): void {
    const spec = this.response()?.indicator_specs.find((item) => item.id === id);
    if (!spec) return;
    if (spec.strategy_default) {
      this.excludedStrategyIndicatorIds.update((items) => [...items, id]);
      return;
    }
    this.userIndicators.update((items) =>
      items.filter((item) => !indicatorMatches(spec.name, spec.params, item.name, item.params)),
    );
  }
}
