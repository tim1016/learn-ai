import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, inject, input, output } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { catchError, map, of } from "rxjs";

import { environment } from "../../../../environments/environment";
import type { RunVerdict } from "../../../api/run-verdict.types";
import {
  BACKTEST_RUN_DETAIL_QUERY,
  type BacktestRunDetail,
  type BacktestRunDetailQueryResult,
  type BacktestRunDetailTrade,
} from "../../../graphql/backtest-runs.query";
import type { TradingCandle, TradingMarker, TradingPoint } from "../../../shared/trading-chart";
import type {
  EngineResultData,
  EngineTrade,
  LeanStatistics,
  LeanAnalysisFinding,
} from "../../lean-engine/engine-results/engine-results.component";
import { StrategyLabChartComponent } from "../../strategy-lab/strategy-lab-chart/strategy-lab-chart.component";
import { StrategyLabDeepDivesComponent } from "../../strategy-lab/strategy-lab-deep-dives/strategy-lab-deep-dives.component";
import { StrategyLabMetricsComponent } from "../../strategy-lab/strategy-lab-metrics/strategy-lab-metrics.component";
import {
  StrategyLabVerdictComponent,
  type StrategyLabParityView,
} from "../../strategy-lab/strategy-lab-verdict/strategy-lab-verdict.component";

interface EngineBar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

interface EngineBarsResponse {
  policy_key: string;
  symbol: string;
  count: number;
  bars: EngineBar[];
  coverage: {
    expected_days: number;
    available_days: number;
    is_complete: boolean;
    missing_days: string[];
  };
}

interface BarsQuery {
  symbol: string;
  from_date: string;
  to_date: string;
  adjusted: boolean;
  session: string;
  timespan: string;
  multiplier: number;
}

type BarsState =
  | { kind: "loaded"; response: EngineBarsResponse }
  | { kind: "unavailable"; reason: string }
  | { kind: "pending" };

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** One persisted-run renderer shared by Workbench and the run-detail route. */
@Component({
  selector: "app-engine-run-report",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    StrategyLabVerdictComponent,
    StrategyLabMetricsComponent,
    StrategyLabChartComponent,
    StrategyLabDeepDivesComponent,
  ],
  templateUrl: "./run-report.component.html",
  styleUrl: "./run-report.component.scss",
})
export class RunReportComponent {
  private readonly apollo = inject(Apollo);
  private readonly http = inject(HttpClient);

  readonly runId = input.required<number>();
  readonly rerun = output();

  private readonly runResource = rxResource<BacktestRunDetail | null, number>({
    params: () => this.runId(),
    stream: ({ params }) => {
      const ref = this.apollo.watchQuery<BacktestRunDetailQueryResult>({
        query: BACKTEST_RUN_DETAIL_QUERY,
        variables: { id: params },
        fetchPolicy: "cache-and-network",
        pollInterval: 5000,
      });
      return ref.valueChanges.pipe(map((result): BacktestRunDetail | null => {
        const run = (result.data?.backtestRun as BacktestRunDetail | null | undefined) ?? null;
        if (!run || !run.parityVerdicts.some((verdict) => verdict.status === "pending")) {
          ref.stopPolling();
        }
        return run;
      }));
    },
  });

  readonly run = computed(() => this.runResource.value() ?? null);
  readonly loading = computed(() => this.runResource.isLoading() && !this.run());

  private readonly barsQuery = computed<BarsQuery | null>(() => {
    const run = this.run();
    const policy = run?.dataPolicy;
    const bars = policy?.strategy_bars;
    if (!run || !policy || !bars || !ISO_DATE.test(run.startDate) || !ISO_DATE.test(run.endDate)) return null;
    return {
      symbol: run.symbol,
      from_date: run.startDate,
      to_date: run.endDate,
      adjusted: policy.adjusted,
      session: policy.session,
      timespan: bars.timespan,
      multiplier: bars.multiplier,
    };
  });

  private readonly barsResource = rxResource<BarsState, BarsQuery | null>({
    params: () => this.barsQuery(),
    stream: ({ params }) => {
      if (params === null) {
        return of<BarsState>({
          kind: "unavailable",
          reason: "Price chart unavailable — this run has no recorded data policy or date window.",
        });
      }
      return this.http
        .get<EngineBarsResponse>(`${environment.pythonServiceUrl}/api/engine/bars`, { params: { ...params } })
        .pipe(
          map((response): BarsState => ({ kind: "loaded", response })),
          catchError(() => of<BarsState>({
            kind: "unavailable",
            reason: "Bar store unreachable — price chart unavailable for this run.",
          })),
        );
    },
  });

  private readonly barsState = computed<BarsState>(() =>
    this.barsResource.value() ?? { kind: "pending" },
  );

  readonly candles = computed<TradingCandle[]>(() => {
    const state = this.barsState();
    if (state.kind !== "loaded") return [];
    return state.response.bars.map((bar) => ({
      timeMs: bar.t,
      open: bar.o,
      high: bar.h,
      low: bar.l,
      close: bar.c,
      volume: bar.v,
    }));
  });

  readonly barsNotice = computed<string | null>(() => {
    const state = this.barsState();
    if (state.kind === "unavailable") return state.reason;
    if (state.kind === "loaded" && !state.response.coverage.is_complete) {
      const coverage = state.response.coverage;
      return `Bar store covers ${coverage.available_days} of ${coverage.expected_days} weekdays; missing sessions remain visible as gaps.`;
    }
    return null;
  });

  readonly verdict = computed<RunVerdict | null>(() => {
    const json = this.run()?.verdictJson;
    if (!json) return null;
    try {
      return JSON.parse(json) as RunVerdict;
    } catch {
      return null;
    }
  });

  readonly engineResult = computed<EngineResultData | null>(() => {
    const run = this.run();
    if (!run) return null;
    const analytics = run.validationAnalytics;
    return {
      success: true,
      strategy_name: run.strategyName,
      fill_mode: run.fillMode,
      initial_cash: run.initialCash,
      final_equity: run.finalEquity,
      net_profit: run.totalPnL,
      total_fees: run.totalFees,
      total_trades: run.totalTrades,
      winning_trades: run.winningTrades,
      losing_trades: run.losingTrades,
      win_rate: run.winRate,
      statistics: {
        max_drawdown_pct: run.maxDrawdown,
        sharpe_ratio: run.sharpeRatio,
        sortino_ratio: run.sortinoRatio,
        profit_factor: run.profitFactor,
        expectancy_pct: null,
      },
      lean_statistics: parseLeanStatistics(run.leanStatisticsJson),
      lean_analysis: parseLeanAnalysis(run.leanAnalysisJson),
      trades: run.trades.map(toEngineTrade),
      log_lines: [],
      validation_analytics: analytics && !analytics.error ? analytics : null,
    };
  });

  readonly markers = computed<TradingMarker[]>(() =>
    (this.run()?.trades ?? []).flatMap((trade) => {
      const won = trade.pnL >= 0;
      const color = won ? "#26a69a" : "#ef5350";
      return [{
        timeMs: trade.entryTimestamp,
        position: "belowBar" as const,
        color,
        shape: "arrowUp" as const,
        text: `BUY · ${won ? "WIN" : "LOSS"}`,
      },
      {
        timeMs: trade.exitTimestamp,
        position: "aboveBar" as const,
        color,
        shape: "arrowDown" as const,
        text: `SELL · ${won ? "+" : ""}${formatMoney(trade.pnL)}`,
      }];
    }),
  );

  readonly equityPoints = computed<TradingPoint[]>(() =>
    this.run()?.equityCurve?.points.map((point) => ({ timeMs: point.t, value: point.e })) ?? [],
  );

  readonly reportNotices = computed<string[]>(() => {
    const run = this.run();
    if (!run) return [];

    const notices: string[] = [];
    const barsNotice = this.barsNotice();
    if (barsNotice) notices.push(barsNotice);

    if (!run.equityCurve) {
      notices.push("Equity curve was not recorded for this run.");
    } else if (run.equityCurve.error) {
      notices.push(run.equityCurve.error);
    } else if (run.source === "lean-sidecar") {
      notices.push(
        "LEAN curve note: this is the native Strategy Equity chart at LEAN's emitted sampling cadence. It can be sparse or end before the terminal portfolio value; headline KPIs come from the native result and closed-trade ledger, never from interpolating this chart.",
      );
    }

    if (!run.validationAnalytics) {
      notices.push("Validation analytics were not recorded for this run.");
    } else if (run.validationAnalytics.error) {
      notices.push(run.validationAnalytics.error);
    }

    return notices;
  });

  readonly parity = computed<StrategyLabParityView | null>(() => {
    const verdicts = this.run()?.parityVerdicts ?? [];
    if (verdicts.length === 0) return null;
    const latest = [...verdicts].sort((left, right) => right.createdAt - left.createdAt)[0];
    return toParityView(latest);
  });
}

export function parseLeanAnalysis(json: string | null): LeanAnalysisFinding[] {
  if (!json) return [];
  try {
    const value: unknown = JSON.parse(json);
    if (!Array.isArray(value)) return [];
    return value.filter((finding): finding is LeanAnalysisFinding => {
      if (!finding || typeof finding !== "object") return false;
      const item = finding as Record<string, unknown>;
      return (
        typeof item["name"] === "string" &&
        typeof item["issue"] === "string" &&
        Array.isArray(item["solutions"]) &&
        item["solutions"].every((solution) => typeof solution === "string")
      );
    });
  } catch {
    return [];
  }
}

const UNAVAILABLE_REASON_COPY: Record<string, string> = {
  no_lean_counterpart: "No LEAN counterpart is registered for this strategy.",
  execution_profile_unsupported:
    "Start a Compatibility pair so both engines use the pinned sizing, fill, and fee contract.",
  adjustment_unsupported: "LEAN validates raw bars only — run with adjusted=false to get a companion.",
  resolution_unsupported: "LEAN companions run on minute resolution only.",
  window_unsupported: "The run has no explicit date window for the companion to reproduce.",
  lean_native_metric_mismatch: "The reproduced LEAN-native statistics differ from LEAN's own result.",
  production_readiness_mismatch: "The two engines produced different production-readiness evidence.",
  lean_native_metric_parity_unavailable: "LEAN-native calculation evidence is incomplete, so agreement cannot be claimed.",
  production_readiness_parity_unavailable: "One run has no comparable production-readiness envelope.",
  trade_reconciliation_diverged: "One or more trades differ between the engines.",
  compatibility_input_mismatch: "The runs did not consume the same pinned data or compatibility settings.",
  compatibility_input_parity_unavailable: "The shared input fixture receipt is incomplete.",
};

function toParityView(
  verdict: { status: string; verdictJson: string; createdAt: number },
): StrategyLabParityView {
  let parsed: {
    reason?: string | null;
    counts_by_category?: Record<string, number>;
    divergences?: StrategyLabParityView["divergences"];
    native_metric_parity?: NonNullable<StrategyLabParityView["nativeMetricParity"]>;
    readiness_parity?: NonNullable<StrategyLabParityView["readinessParity"]>;
    input_parity?: NonNullable<StrategyLabParityView["inputParity"]>;
  } = {};
  try {
    parsed = JSON.parse(verdict.verdictJson) as typeof parsed;
  } catch {
    // Status-only is the honest fallback for an unreadable legacy payload.
  }
  const reason = parsed.reason ?? null;
  return {
    status: verdict.status,
    createdAt: verdict.createdAt,
    reason: reason ? (UNAVAILABLE_REASON_COPY[reason] ?? reason) : null,
    countsByCategory: Object.entries(parsed.counts_by_category ?? {}).map(([category, count]) => ({ category, count })),
    divergences: parsed.divergences ?? [],
    nativeMetricParity: parsed.native_metric_parity ?? null,
    readinessParity: parsed.readiness_parity ?? null,
    inputParity: parsed.input_parity ?? null,
  };
}

export function toEngineTrade(trade: BacktestRunDetailTrade, index: number): EngineTrade {
  return {
    trade_number: index + 1,
    entry_time: trade.entryTimestamp,
    entry_price: trade.entryPrice,
    exit_time: trade.exitTimestamp,
    exit_price: trade.exitPrice,
    quantity: trade.quantity,
    indicators: {},
    pnl_pts: trade.pnlPts,
    pnl_pct: trade.pnlPct,
    result: trade.pnL > 0 ? "WIN" : "LOSS",
    signal_reason: trade.signalReason,
  };
}

function parseLeanStatistics(json: string | null): LeanStatistics | null {
  if (!json) return null;
  try {
    const parsed = JSON.parse(json) as LeanStatistics;
    return parsed?.portfolio && parsed.trade && parsed.runtime ? parsed : null;
  } catch {
    return null;
  }
}

function formatMoney(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}
