import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { catchError, map, of } from "rxjs";

import { environment } from "../../../../environments/environment";
import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { IndicatorCatalogService } from "../../../shared/indicator-catalog/indicator-catalog.service";
import type { IndicatorPickerAdd } from "../../../shared/indicator-picker/indicator-picker.component";
import { MarketDataService } from "../../../services/market-data.service";
import {
  TradingChartComponent,
  type TradingCandle,
  type TradingIndicatorChip,
  type TradingMarker,
  type TradingPoint,
  type TradingSeries,
  type TradingSubPane,
} from "../../../shared/trading-chart";
import {
  SERIES_COLORS,
  indicatorId,
  indicatorLabel,
  normalizeIndicatorResults,
  strategyIndicatorRequests,
  timeframeKey,
  type ChartIndicatorResult,
  type StrategyIndicatorRequest,
} from "./strategy-lab-chart.adapter";

interface ChartIndicatorResponse {
  indicators: ChartIndicatorResult[];
}

interface IndicatorQuery {
  ticker: string;
  from_date: string;
  to_date: string;
  timeframe: string;
  session: string;
  adjusted: boolean;
  indicators: { name: string; params: Record<string, number> }[];
}

type IndicatorState =
  | { kind: "loaded"; results: ChartIndicatorResult[] }
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
  protected readonly catalog = inject(IndicatorCatalogService);
  private readonly marketData = inject(MarketDataService);

  readonly run = input.required<BacktestRunDetail>();
  readonly candles = input<readonly TradingCandle[]>([]);
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly equityPoints = input<readonly TradingPoint[]>([]);

  private readonly activeRequests = signal<StrategyIndicatorRequest[]>([]);
  private seededRunId: number | null = null;

  constructor() {
    void this.catalog.load();
    effect(() => {
      const run = this.run();
      if (run.id === this.seededRunId) return;
      this.seededRunId = run.id;
      this.activeRequests.set(strategyIndicatorRequests(run.strategyName, run.parameters));
    });
  }

  private readonly indicatorQuery = computed<IndicatorQuery | null>(() => {
    const run = this.run();
    const policy = run.dataPolicy;
    const bars = policy?.strategy_bars;
    const active = this.activeRequests();
    if (!policy || !bars || active.length === 0) return null;
    return {
      ticker: run.symbol,
      from_date: run.startDate,
      to_date: run.endDate,
      timeframe: timeframeKey(bars.timespan, bars.multiplier),
      session: policy.session === "regular" ? "rth" : "extended",
      adjusted: policy.adjusted,
      indicators: active.map(({ name, params }) => ({ name, params })),
    };
  });

  private readonly indicatorResource = rxResource<IndicatorState, IndicatorQuery | null>({
    params: () => this.indicatorQuery(),
    stream: ({ params }) => {
      if (params === null) return of<IndicatorState>({ kind: "loaded", results: [] });
      return this.http
        .post<ChartIndicatorResponse>(`${environment.pythonServiceUrl}/api/chart/data`, {
          ...params,
          forward_fill: false,
          compute_all_indicators: false,
        })
        .pipe(
          map((response): IndicatorState => ({ kind: "loaded", results: response.indicators })),
          catchError(() => of<IndicatorState>({
            kind: "unavailable",
            message: "Signal indicators are unavailable; price, trades, and equity remain synchronized.",
          })),
        );
    },
  });

  private readonly quoteResource = rxResource({
    params: () => this.run().symbol,
    stream: ({ params }) => this.marketData.getStockSnapshot(params).pipe(catchError(() => of(null))),
  });

  readonly quote = computed(() => {
    const run = this.run();
    const snapshot = this.quoteResource.value()?.snapshot;
    const lastCandle = this.candles()[this.candles().length - 1];
    const price = snapshot?.min?.close ?? snapshot?.day?.close ?? lastCandle?.close;
    if (price === null || price === undefined) return null;
    return {
      ticker: run.symbol,
      price,
      change: snapshot?.todaysChange ?? null,
      changePercent: snapshot?.todaysChangePercent ?? null,
    };
  });

  readonly indicatorNotice = computed(() => {
    const state = this.indicatorResource.value();
    return state?.kind === "unavailable" ? state.message : null;
  });

  private readonly indicatorResults = computed(() => {
    const state = this.indicatorResource.value();
    return state?.kind === "loaded" ? state.results : [];
  });

  private readonly normalizedIndicators = computed(() =>
    normalizeIndicatorResults(this.indicatorResults()),
  );

  readonly overlays = computed<TradingSeries[]>(() => this.normalizedIndicators().overlays);

  readonly subPanes = computed<TradingSubPane[]>(() => this.normalizedIndicators().subPanes);

  readonly equity = computed<TradingSeries[]>(() => this.equityPoints().length === 0 ? [] : [{
    id: "equity",
    name: "Equity",
    color: "#7aa9ff",
    type: "area",
    points: [...this.equityPoints()],
  }]);

  readonly activeIndicators = computed<TradingIndicatorChip[]>(() => {
    const results = this.indicatorResults();
    return this.activeRequests().map((request, index) => ({
      id: request.id,
      label: request.label,
      color: results[index]?.color ?? SERIES_COLORS[index % SERIES_COLORS.length],
    }));
  });

  readonly timeframeLabel = computed(() => {
    const bars = this.run().dataPolicy?.strategy_bars;
    return bars ? timeframeKey(bars.timespan, bars.multiplier) : "1m";
  });

  addIndicator(event: IndicatorPickerAdd): void {
    const baseId = indicatorId(event.name, event.params);
    const occurrence = this.activeRequests().filter((item) => item.id.startsWith(baseId)).length + 1;
    const id = occurrence === 1 ? baseId : `${baseId}#${occurrence}`;
    this.activeRequests.update((items) => [...items, {
      id,
      name: event.name,
      params: { ...event.params },
      label: indicatorLabel(event.name, event.params),
    }]);
  }

  removeIndicator(id: string): void {
    this.activeRequests.update((items) => items.filter((item) => item.id !== id));
  }
}
