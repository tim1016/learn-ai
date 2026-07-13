import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, inject, input } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { catchError, map, of } from "rxjs";

import { environment } from "../../../../environments/environment";
import type { RunVerdict } from "../../../api/run-verdict.types";
import {
  BACKTEST_RUN_DETAIL_QUERY,
  BacktestRunDetail,
  BacktestRunDetailQueryResult,
  BacktestRunDetailTrade,
} from "../../../graphql/backtest-runs.query";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent, TimestampDisplayPipe } from "../../../shared/timestamp";
import type {
  ChartBar,
  EngineTradeForChart,
  EquityCurvePoint,
} from "../../lean-engine/engine-chart/engine-chart.component";
import {
  EngineResultData,
  EngineResultsComponent,
  EngineTrade,
  LeanStatistics,
} from "../../lean-engine/engine-results/engine-results.component";

/** Wire shape of GET /api/engine/bars (PythonDataService). */
interface EngineBarsResponse {
  policy_key: string;
  symbol: string;
  count: number;
  bars: ChartBar[];
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

/**
 * The single run report: renders a persisted run — and only a persisted
 * run — through the same component tree for the workbench post-run stage
 * and the /engine/runs/:id route. Bars come from the shared bar store
 * (the bytes the engine consumed); everything else comes from the
 * persisted row. There is no transient-payload render path.
 */
@Component({
  selector: "app-engine-run-report",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [EngineResultsComponent, ReceiptLabelPipe, TimestampDisplayComponent, TimestampDisplayPipe],
  templateUrl: "./run-report.component.html",
  styleUrls: ["./run-report.component.scss"],
})
export class RunReportComponent {
  private readonly apollo = inject(Apollo);
  private readonly http = inject(HttpClient);

  readonly runId = input.required<number>();

  private readonly runResource = rxResource<BacktestRunDetail | null, number>({
    params: () => this.runId(),
    stream: ({ params }) => {
      // Poll while a parity verdict is pending (the async LEAN companion
      // is still running); stop the moment every verdict is terminal so
      // settled reports cost nothing.
      const ref = this.apollo.watchQuery<BacktestRunDetailQueryResult>({
        query: BACKTEST_RUN_DETAIL_QUERY,
        variables: { id: params },
        fetchPolicy: "cache-and-network",
        pollInterval: 5000,
      });
      return ref.valueChanges.pipe(
        map((result): BacktestRunDetail | null => {
          const run = (result.data?.backtestRun as BacktestRunDetail | null | undefined) ?? null;
          if (!run || !run.parityVerdicts.some((v) => v.status === "pending")) {
            ref.stopPolling();
          }
          return run;
        }),
      );
    },
  });

  readonly run = computed(() => this.runResource.value() ?? null);
  readonly loading = computed(() => this.runResource.isLoading() && !this.run());

  /** Bars request derived from the persisted DataPolicy — the report
   *  charts exactly the policy the run recorded, or nothing. */
  private readonly barsQuery = computed<BarsQuery | null>(() => {
    const run = this.run();
    const policy = run?.dataPolicy;
    const bars = policy?.strategy_bars;
    if (!run || !policy || !bars) return null;
    if (!ISO_DATE.test(run.startDate) || !ISO_DATE.test(run.endDate)) return null;
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
        .get<EngineBarsResponse>(`${environment.pythonServiceUrl}/api/engine/bars`, {
          params: { ...params },
        })
        .pipe(
          map((response): BarsState => ({ kind: "loaded", response })),
          catchError(() =>
            of<BarsState>({
              kind: "unavailable",
              reason: "Bar store unreachable — price chart unavailable for this run.",
            }),
          ),
        );
    },
  });

  private readonly barsState = computed<BarsState>(() => this.barsResource.value() ?? { kind: "pending" });

  readonly chartBars = computed<ChartBar[]>(() => {
    const state = this.barsState();
    return state.kind === "loaded" ? state.response.bars : [];
  });

  readonly barsNotice = computed<string | null>(() => {
    const state = this.barsState();
    if (state.kind === "unavailable") return state.reason;
    if (state.kind === "loaded" && !state.response.coverage.is_complete) {
      const c = state.response.coverage;
      return `Bar store covers ${c.available_days} of ${c.expected_days} weekdays in this window — missing days are shown as gaps, not fetched.`;
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
        // Not persisted as a column — rendered as an honest dash.
        expectancy_pct: null,
      },
      lean_statistics: parseLeanStatistics(run.leanStatisticsJson),
      trades: run.trades.map(toEngineTrade),
      log_lines: [],
      validation_analytics: analytics && !analytics.error ? analytics : null,
    };
  });

  readonly chartTrades = computed<EngineTradeForChart[]>(() =>
    (this.run()?.trades ?? []).map((t) => ({
      entry_time: t.entryTimestamp,
      exit_time: t.exitTimestamp,
      entry_price: t.entryPrice,
      exit_price: t.exitPrice,
      pnl_pts: t.exitPrice - t.entryPrice,
      result: t.pnL > 0 ? "WIN" : "LOSS",
    })),
  );

  readonly equityCurve = computed<EquityCurvePoint[]>(
    () => this.run()?.equityCurve?.points.map((p) => ({ timestamp: p.t, equity: p.e })) ?? [],
  );

  readonly equityNotice = computed<string | null>(() => {
    const curve = this.run()?.equityCurve;
    if (!curve) return "Equity curve not recorded for this run.";
    if (curve.error) return curve.error;
    return null;
  });

  readonly analyticsNotice = computed<string | null>(() => {
    const run = this.run();
    if (!run) return null;
    if (run.validationAnalytics?.error) return run.validationAnalytics.error;
    if (!run.validationAnalytics) return "Validation analytics not recorded for this run.";
    return null;
  });

  /** Latest parity disposition for this run (a run appears on the left
   *  side as the Python original or the right side as the LEAN
   *  companion — both render the same frozen verdict). */
  readonly parity = computed<ParityView | null>(() => {
    const verdicts = this.run()?.parityVerdicts ?? [];
    if (verdicts.length === 0) return null;
    const latest = [...verdicts].sort((a, b) => b.createdAt - a.createdAt)[0];
    return toParityView(latest);
  });

  readonly resolutionLabel = computed<string>(() => {
    const bars = this.run()?.dataPolicy?.strategy_bars;
    if (!bars) return "";
    const unit = bars.timespan === "minute" ? "m" : bars.timespan === "hour" ? "h" : "d";
    return `${bars.multiplier}${unit}`;
  });
}

/** Template view-model for the parity panel. */
export interface ParityView {
  status: string;
  createdAt: number;
  reason: string | null;
  countsByCategory: { category: string; count: number }[];
  divergences: ParityDivergenceView[];
}

export interface ParityDivergenceView {
  category: string;
  trade_number: number | null;
  ms_utc: number | null;
  message: string;
}

const UNAVAILABLE_REASON_COPY: Record<string, string> = {
  no_lean_counterpart: "No LEAN counterpart is registered for this strategy.",
  adjustment_unsupported: "LEAN validates raw bars only — run with adjusted=false to get a companion.",
  resolution_unsupported: "LEAN companions run on minute resolution only.",
  window_unsupported: "The run has no explicit date window for the companion to reproduce.",
};

function toParityView(verdict: { status: string; verdictJson: string; createdAt: number }): ParityView {
  let parsed: {
    reason?: string | null;
    counts_by_category?: Record<string, number>;
    divergences?: ParityDivergenceView[];
  } = {};
  try {
    parsed = JSON.parse(verdict.verdictJson) as typeof parsed;
  } catch {
    // Unreadable verdict JSON degrades to status-only display.
  }
  const rawReason = parsed.reason ?? null;
  return {
    status: verdict.status,
    createdAt: verdict.createdAt,
    reason: rawReason ? (UNAVAILABLE_REASON_COPY[rawReason] ?? rawReason) : null,
    countsByCategory: Object.entries(parsed.counts_by_category ?? {}).map(([category, count]) => ({
      category,
      count,
    })),
    divergences: parsed.divergences ?? [],
  };
}

export function toEngineTrade(trade: BacktestRunDetailTrade, index: number): EngineTrade {
  // Display-only arithmetic mirroring the engine's per-trade convention
  // (pnl_pts = exit − entry; pnl_pct relative to entry price). The
  // authoritative dollar PnL stays the persisted ``pnL`` column.
  const pnlPts = trade.exitPrice - trade.entryPrice;
  return {
    trade_number: index + 1,
    entry_time: trade.entryTimestamp,
    entry_price: trade.entryPrice,
    exit_time: trade.exitTimestamp,
    exit_price: trade.exitPrice,
    quantity: trade.quantity,
    indicators: {},
    pnl_pts: pnlPts,
    pnl_pct: trade.entryPrice > 0 ? pnlPts / trade.entryPrice : 0,
    result: trade.pnL > 0 ? "WIN" : "LOSS",
    signal_reason: trade.signalReason,
  };
}

function parseLeanStatistics(json: string | null): LeanStatistics | null {
  if (!json) return null;
  try {
    const parsed = JSON.parse(json) as LeanStatistics;
    return parsed?.portfolio && parsed?.trade && parsed?.runtime ? parsed : null;
  } catch {
    return null;
  }
}
