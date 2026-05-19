import { CurrencyPipe, DatePipe, PercentPipe } from "@angular/common";
import { ChangeDetectionStrategy, Component, computed, inject } from "@angular/core";
import { toSignal } from "@angular/core/rxjs-interop";
import { ActivatedRoute } from "@angular/router";
import { Apollo } from "apollo-angular";
import { map, of, switchMap } from "rxjs";
import {
  COMPARE_BACKTEST_RUNS_QUERY,
  type CompareBacktestRunsQueryResult,
  type CompareBacktestRunsQueryVariables,
  type RunComparisonResult,
} from "../../graphql/compare-backtest-runs.query";

interface ParsedIds {
  leftId: number;
  rightId: number;
}

@Component({
  selector: "app-run-comparison",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DatePipe, PercentPipe],
  templateUrl: "./run-comparison.component.html",
  styleUrl: "./run-comparison.component.scss",
})
export class RunComparisonComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly apollo = inject(Apollo);

  protected readonly data = toSignal<RunComparisonResult | null>(
    this.route.queryParamMap.pipe(
      map((p) => this.parseIds(p.get("left"), p.get("right"))),
      switchMap((ids) => {
        if (ids === null) return of(null);
        return this.apollo
          .watchQuery<CompareBacktestRunsQueryResult, CompareBacktestRunsQueryVariables>({
            query: COMPARE_BACKTEST_RUNS_QUERY,
            variables: ids,
          })
          .valueChanges.pipe(
            map((r): RunComparisonResult | null =>
              (r.data?.compareBacktestRuns as RunComparisonResult | null | undefined) ?? null,
            ),
          );
      }),
    ),
  );

  protected readonly warnings = computed(() => this.data()?.guardrails.warnings ?? []);
  protected readonly isEmpty = computed(() => this.data() === null);

  private parseIds(leftRaw: string | null, rightRaw: string | null): ParsedIds | null {
    if (leftRaw === null || rightRaw === null) return null;
    const leftId = Number.parseInt(leftRaw, 10);
    const rightId = Number.parseInt(rightRaw, 10);
    if (!Number.isFinite(leftId) || !Number.isFinite(rightId)) return null;
    return { leftId, rightId };
  }
}
