import { CurrencyPipe, PercentPipe } from "@angular/common";
import { ChangeDetectionStrategy, Component, computed, inject } from "@angular/core";
import { toSignal } from "@angular/core/rxjs-interop";
import { ActivatedRoute, RouterLink } from "@angular/router";
import { Apollo } from "apollo-angular";
import { map, of, switchMap } from "rxjs";

import {
  BACKTEST_RUN_DETAIL_QUERY,
  BacktestRunDetail,
  BacktestRunDetailQueryResult,
} from "../../../graphql/backtest-runs.query";
import { TimestampDisplayPipe } from "../../../shared/timestamp";

interface ParsedVerdict {
  headline: string;
  grade: string | null;
  signal: string | null;
  composite: number | null;
  missing_metrics: string[];
}

@Component({
  selector: "app-engine-run-detail",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, PercentPipe, RouterLink, TimestampDisplayPipe],
  templateUrl: "./engine-run-detail.component.html",
  styleUrl: "./engine-run-detail.component.scss",
})
export class EngineRunDetailComponent {
  private readonly apollo = inject(Apollo);
  private readonly route = inject(ActivatedRoute);

  private readonly result = toSignal(
    this.route.paramMap.pipe(
      map((params) => Number(params.get("id"))),
      switchMap((id) => {
        if (!Number.isFinite(id) || id <= 0) {
          return of({ data: { backtestRun: null }, loading: false });
        }
        return this.apollo.watchQuery<BacktestRunDetailQueryResult>({
          query: BACKTEST_RUN_DETAIL_QUERY,
          variables: { id },
          fetchPolicy: "cache-and-network",
        }).valueChanges;
      }),
    ),
    { initialValue: { data: { backtestRun: null }, loading: true } },
  );

  readonly run = computed(() => (this.result().data?.backtestRun ?? null) as BacktestRunDetail | null);
  readonly loading = computed(() => this.result().loading);
  readonly verdict = computed(() => this.parseVerdict(this.run()));
  readonly equityReceipt = computed(() => this.parseEquityReceipt(this.run()));
  readonly equityCurve = computed(() => this.run()?.equityCurve ?? []);
  readonly trades = computed(() => this.run()?.trades ?? []);

  private parseVerdict(run: BacktestRunDetail | null): ParsedVerdict | null {
    if (!run?.verdictJson) return null;
    try {
      const parsed = JSON.parse(run.verdictJson) as Partial<ParsedVerdict>;
      return {
        headline: typeof parsed.headline === "string" ? parsed.headline : "Run verdict unavailable.",
        grade: typeof parsed.grade === "string" ? parsed.grade : null,
        signal: typeof parsed.signal === "string" ? parsed.signal : null,
        composite: typeof parsed.composite === "number" ? parsed.composite : null,
        missing_metrics: Array.isArray(parsed.missing_metrics) ? parsed.missing_metrics : [],
      };
    } catch {
      return null;
    }
  }

  private parseEquityReceipt(run: BacktestRunDetail | null): string {
    if (!run?.equityCurveJson) return "Equity curve not recorded.";
    try {
      const parsed = JSON.parse(run.equityCurveJson) as {
        cadence?: string;
        downsample?: { raw_points?: number; kept_points?: number };
      };
      const cadence = parsed.cadence ?? "unknown";
      const raw = parsed.downsample?.raw_points ?? run.equityCurve.length;
      const kept = parsed.downsample?.kept_points ?? run.equityCurve.length;
      return `${cadence}: ${kept} of ${raw} points`;
    } catch {
      return "Equity receipt unreadable.";
    }
  }
}
