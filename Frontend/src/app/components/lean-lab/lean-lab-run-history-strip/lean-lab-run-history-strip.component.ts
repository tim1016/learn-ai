import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  output,
  signal,
} from "@angular/core";
import { toSignal } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { map } from "rxjs/operators";

import {
  BACKTEST_RUNS_QUERY,
  BacktestRunNode,
  BacktestRunsQueryResult,
  toRunHistoryRow,
} from "../../../graphql/backtest-runs.query";
import { RunHistoryRow } from "../../shared/run-history/run-history.types";
import { LeanLabRunHistoryComponent } from "../lean-lab-run-history/lean-lab-run-history.component";

@Component({
  selector: "app-lean-lab-run-history-strip",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [LeanLabRunHistoryComponent],
  templateUrl: "./lean-lab-run-history-strip.component.html",
  styleUrl: "./lean-lab-run-history-strip.component.scss",
})
export class LeanLabRunHistoryStripComponent {
  private readonly apollo = inject(Apollo);

  readonly runSelected = output<string>();
  readonly isExpanded = signal(false);

  private readonly queryRef = this.apollo.watchQuery<BacktestRunsQueryResult>({
    query: BACKTEST_RUNS_QUERY,
    variables: { engine: "LEAN_SIDECAR", first: 50 },
    fetchPolicy: "cache-and-network",
  });

  private readonly rows = toSignal(
    this.queryRef.valueChanges.pipe(
      map((r) => {
        const nodes = r.data?.backtestRuns?.nodes;
        if (!nodes) return [] as RunHistoryRow[];
        return (nodes as BacktestRunNode[]).map(toRunHistoryRow);
      }),
    ),
    { initialValue: [] as RunHistoryRow[] },
  );

  readonly mostRecent = computed<RunHistoryRow | null>(() => this.rows()[0] ?? null);
  readonly runCount = computed<number>(() => this.rows().length);

  toggle(): void {
    this.isExpanded.update((v) => !v);
  }

  onRunSelected(id: string): void {
    this.runSelected.emit(id);
  }
}
