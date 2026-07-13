import { CurrencyPipe, PercentPipe } from "@angular/common";
import { ChangeDetectionStrategy, Component, computed, inject, input } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { RouterLink } from "@angular/router";
import { Apollo } from "apollo-angular";
import { map, of } from "rxjs";

import type { RunVerdict } from "../../../api/run-verdict.types";
import {
  BACKTEST_RUN_DETAIL_QUERY,
  BacktestRunDetail,
  BacktestRunDetailQueryResult,
} from "../../../graphql/backtest-runs.query";
import { TimestampDisplayPipe } from "../../../shared/timestamp";

type VerdictState =
  | { kind: "ready"; verdict: RunVerdict }
  | { kind: "missing" }
  | { kind: "unreadable" };

@Component({
  selector: "app-engine-run-detail",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, PercentPipe, RouterLink, TimestampDisplayPipe],
  templateUrl: "./engine-run-detail.component.html",
  styleUrls: ["./engine-run-detail.component.scss"],
})
export class EngineRunDetailComponent {
  private readonly apollo = inject(Apollo);
  readonly id = input<string | null>(null);

  private readonly runResource = rxResource<BacktestRunDetail | null, number | null>({
    params: () => {
      const parsed = Number(this.id());
      return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    },
    stream: ({ params }) => {
      if (params === null) {
        return of(null);
      }
      return this.apollo.watchQuery<BacktestRunDetailQueryResult>({
          query: BACKTEST_RUN_DETAIL_QUERY,
          variables: { id: params },
          fetchPolicy: "cache-and-network",
        }).valueChanges.pipe(
          map((result): BacktestRunDetail | null => (result.data?.backtestRun as BacktestRunDetail | null | undefined) ?? null),
        );
    },
  });

  readonly run = computed(() => this.runResource.value() ?? null);
  readonly loading = computed(() => this.runResource.isLoading());
  readonly verdict = computed(() => this.parseVerdict(this.run()));
  readonly equityReceipt = computed(() => {
    const curve = this.run()?.equityCurve;
    if (!curve) return "Equity curve not recorded.";
    if (curve.error) return curve.error;
    return `${curve.cadence ?? "unknown"}: ${curve.keptPoints} of ${curve.rawPoints} points`;
  });
  readonly equityCurve = computed(() => this.run()?.equityCurve?.points ?? []);
  readonly trades = computed(() => this.run()?.trades ?? []);
  readonly visibleTrades = computed(() => this.trades().slice(0, 12));
  readonly tradeReceipt = computed(() => {
    const total = this.trades().length;
    const visible = this.visibleTrades().length;
    return visible === total ? `${total} rows` : `showing ${visible} of ${total}`;
  });

  private parseVerdict(run: BacktestRunDetail | null): VerdictState {
    if (!run?.verdictJson) return { kind: "missing" };
    try {
      const parsed = JSON.parse(run.verdictJson) as unknown;
      return isRunVerdict(parsed) ? { kind: "ready", verdict: parsed } : { kind: "unreadable" };
    } catch {
      return { kind: "unreadable" };
    }
  }
}

function isRunVerdict(value: unknown): value is RunVerdict {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Partial<RunVerdict>;
  return (
    typeof candidate.verdict_version === "number" &&
    typeof candidate.engine === "string" &&
    typeof candidate.generated_at_ms === "number" &&
    typeof candidate.headline === "string" &&
    Array.isArray(candidate.red_flags) &&
    Array.isArray(candidate.dimensions) &&
    Array.isArray(candidate.missing_metrics)
  );
}
