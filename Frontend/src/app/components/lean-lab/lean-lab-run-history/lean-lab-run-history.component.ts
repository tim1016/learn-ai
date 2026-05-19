import { ChangeDetectionStrategy, Component, inject, output } from "@angular/core";
import { Router } from "@angular/router";
import { Apollo } from "apollo-angular";
import { toSignal } from "@angular/core/rxjs-interop";
import { map } from "rxjs/operators";

import { RunHistoryComponent } from "../../shared/run-history/run-history.component";
import {
  BACKTEST_RUNS_QUERY,
  BacktestRunNode,
  BacktestRunsQueryResult,
  toRunHistoryRow,
} from "../../../graphql/backtest-runs.query";

@Component({
  selector: "app-lean-lab-run-history",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RunHistoryComponent],
  template: `
    <app-run-history
      [rows]="rows() ?? []"
      [allowCompare]="true"
      (compareRequested)="onCompare($event)"
      (runSelected)="onRowSelected($event)" />
  `,
})
export class LeanLabRunHistoryComponent {
  private readonly apollo = inject(Apollo);
  private readonly router = inject(Router);

  readonly runSelected = output<string>();

  private readonly queryRef = this.apollo.watchQuery<BacktestRunsQueryResult>({
    query: BACKTEST_RUNS_QUERY,
    variables: { engine: "LEAN_SIDECAR", first: 50 },
    fetchPolicy: "cache-and-network",
  });

  readonly rows = toSignal(
    this.queryRef.valueChanges.pipe(
      map((r) => {
        const nodes = r.data?.backtestRuns?.nodes;
        if (!nodes) return [];
        return (nodes as BacktestRunNode[]).map(toRunHistoryRow);
      }),
    ),
    { initialValue: [] },
  );

  onRowSelected(rowId: string): void {
    const row = this.rows().find((r) => r.id === rowId);
    const leanRunId = row?.leanRunId;
    if (leanRunId) {
      this.runSelected.emit(leanRunId);
    }
  }

  onCompare(event: { leftId: string; rightId: string }): void {
    void this.router.navigate(["/runs/compare"], {
      queryParams: { left: event.leftId, right: event.rightId },
    });
  }
}

