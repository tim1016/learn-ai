import { ChangeDetectionStrategy, Component, computed, inject, input } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { filter, map, of } from "rxjs";

import {
  BACKTEST_RUN_DETAIL_QUERY,
  type BacktestRunDetail,
  type BacktestRunDetailQueryResult,
  type BacktestRunDetailTrade,
} from "../../../graphql/backtest-runs.query";
import type { TradingMarker, TradingPoint } from "../../../shared/trading-chart";
import type {
  EngineResultData,
  EngineTrade,
  LeanStatistics,
  LeanAnalysisFinding,
} from "../../lean-engine/engine-results/engine-results.component";
import { StrategyLabChartComponent } from "../../strategy-lab/strategy-lab-chart/strategy-lab-chart.component";
import { StrategyLabDeepDivesComponent } from "../../strategy-lab/strategy-lab-deep-dives/strategy-lab-deep-dives.component";
import { ResultsSummaryComponent } from "../../strategy-lab/results-summary/results-summary.component";
import { ResultsSidebarComponent } from "../../strategy-lab/results-sidebar/results-sidebar.component";
import {
  parseRunVerdictEnvelope,
  type StrategyLabParityView,
} from "../../strategy-lab/strategy-lab.models";

/** Persisted-run evidence rendered inside the read-only Strategy Lab Results page. */
@Component({
  selector: "app-engine-run-report",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ResultsSummaryComponent,
    ResultsSidebarComponent,
    StrategyLabChartComponent,
    StrategyLabDeepDivesComponent,
  ],
  templateUrl: "./run-report.component.html",
  styleUrl: "./run-report.component.scss",
})
export class RunReportComponent {
  private readonly apollo = inject(Apollo);

  readonly runId = input.required<number>();
  readonly runDetail = input<BacktestRunDetail | null>(null);

  private readonly runResource = rxResource<BacktestRunDetail | null, number | null>({
    params: () => {
      const supplied = this.runDetail();
      return supplied?.id === this.runId() && !hasPendingParity(supplied) ? null : this.runId();
    },
    stream: ({ params }) => {
      if (params === null) return of(null);
      const ref = this.apollo.watchQuery<BacktestRunDetailQueryResult>({
        query: BACKTEST_RUN_DETAIL_QUERY,
        variables: { id: params },
        fetchPolicy: "network-only",
        pollInterval: 5000,
      });
      return ref.valueChanges.pipe(
        filter((result) => !result.loading),
        map((result): BacktestRunDetail | null => {
          // Apollo can surface a GraphQL validation error alongside an empty
          // result. Propagate it to rxResource so an unavailable report is not
          // incorrectly presented as a missing run.
          if (result.error) {
            ref.stopPolling();
            throw result.error;
          }
          const run = (result.data?.backtestRun as BacktestRunDetail | null | undefined) ?? null;
          if (!run || !run.parityVerdicts.some((verdict) => verdict.status === "pending")) {
            ref.stopPolling();
          }
          return run;
        }),
      );
    },
  });

  readonly run = computed(() => {
    const refreshed = this.runResource.hasValue() ? this.runResource.value() : null;
    if (refreshed?.id === this.runId()) return refreshed;
    const supplied = this.runDetail();
    return supplied?.id === this.runId() ? supplied : null;
  });
  readonly loading = computed(() => this.runResource.isLoading() && !this.run());
  readonly loadError = computed(() => this.runResource.error());

  readonly verdictEnvelope = computed(() => parseRunVerdictEnvelope(this.run()?.verdictJson ?? null));
  readonly verdict = computed(() => this.verdictEnvelope().verdict);

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
      const outcome = trade.pnL > 0 ? "WIN" : trade.pnL < 0 ? "LOSS" : "BREAK EVEN";
      const color = trade.pnL > 0 ? "#26a69a" : trade.pnL < 0 ? "#ef5350" : "#90a4ae";
      return [{
        timeMs: trade.entryTimestamp,
        position: "belowBar" as const,
        color,
        shape: "arrowUp" as const,
        text: `BUY · ${outcome}`,
      },
      {
        timeMs: trade.exitTimestamp,
        position: "aboveBar" as const,
        color,
        shape: "arrowDown" as const,
        text: `SELL · ${trade.pnL > 0 ? "+" : ""}${formatMoney(trade.pnL)}`,
      }];
    }),
  );

  readonly equityPoints = computed<TradingPoint[]>(() =>
    this.run()?.equityCurve?.realized?.points.map((point) => ({ timeMs: point.t, value: point.e })) ?? [],
  );

  readonly reportNotices = computed<string[]>(() => {
    const run = this.run();
    if (!run) return [];

    const notices: string[] = [];
    const verdictError = this.verdictEnvelope().error;
    if (verdictError) notices.push(verdictError);
    if (!run.equityCurve) {
      notices.push("This run has no strict dual-curve report.");
    } else if (run.equityCurve.error) {
      notices.push(run.equityCurve.error);
    } else if (run.equityCurve.realized?.error) {
      notices.push(run.equityCurve.realized.error);
    } else if (run.equityCurve.markToMarket?.error) {
      notices.push(run.equityCurve.markToMarket.error);
    } else if (run.source === "lean-sidecar") {
      notices.push(
        "The realized-equity staircase books net P&L only at exits. Native LEAN mark-to-market evidence remains available for its canonical risk statistics and audit receipt.",
      );
    }

    if (!run.validationAnalytics) {
      notices.push("Validation analytics were not recorded for this run.");
    } else if (run.validationAnalytics.error) {
      notices.push(run.validationAnalytics.error);
    }

    if (run.tradesTruncated) {
      notices.push(
        `Showing the most recent ${run.trades.length.toLocaleString()} of ${run.totalTrades.toLocaleString()} trades in the chart markers and ledger. Headline metrics use the complete persisted run.`,
      );
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

function hasPendingParity(run: BacktestRunDetail): boolean {
  return run.parityVerdicts.some((verdict) => verdict.status === "pending");
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
