import { ChangeDetectionStrategy, Component, inject } from "@angular/core";
import { Router } from "@angular/router";
import { Apollo } from "apollo-angular";
import { toSignal } from "@angular/core/rxjs-interop";
import { map } from "rxjs/operators";

import { RunHistoryComponent } from "../../shared/run-history/run-history.component";
import { RunHistoryRow } from "../../shared/run-history/run-history.types";
import {
  BACKTEST_RUNS_QUERY,
  BacktestRunNode,
  BacktestRunsQueryResult,
} from "../../../graphql/backtest-runs.query";

@Component({
  selector: "app-lean-lab-run-history",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RunHistoryComponent],
  template: `
    <app-run-history
      [rows]="rows() ?? []"
      [allowCompare]="true"
      (compareRequested)="onCompare($event)" />
  `,
})
export class LeanLabRunHistoryComponent {
  private readonly apollo = inject(Apollo);
  private readonly router = inject(Router);

  private readonly queryRef = this.apollo.watchQuery<BacktestRunsQueryResult>({
    query: BACKTEST_RUNS_QUERY,
    variables: { engine: "LEAN_SIDECAR", first: 50 },
    fetchPolicy: "cache-and-network",
  });

  readonly rows = toSignal(
    this.queryRef.valueChanges.pipe(
      map((r) => {
        const nodes = r.data?.backtestRuns?.nodes;
        if (!nodes) return [] as RunHistoryRow[];
        return (nodes as BacktestRunNode[]).map(toRunHistoryRow);
      }),
    ),
    { initialValue: [] as RunHistoryRow[] },
  );

  onCompare(event: { leftId: string; rightId: string }): void {
    void this.router.navigate(["/runs/compare"], {
      queryParams: { left: event.leftId, right: event.rightId },
    });
  }
}

function toRunHistoryRow(node: BacktestRunNode): RunHistoryRow {
  return {
    id: node.id,
    source: node.source,
    strategyName: node.strategyName,
    symbol: extractSymbol(node.parameters),
    startDate: node.startDate,
    endDate: node.endDate,
    executedAt: node.executedAt,
    totalTrades: node.totalTrades,
    totalPnl: node.totalPnL,
    hasSyntheticExit: node.trades.some((t) => t.isSyntheticExit),
    leanRunId: node.leanRunId,
  };
}

function extractSymbol(parameters: string | null): string | null {
  if (!parameters) return null;
  try {
    const parsed = JSON.parse(parameters) as { symbol?: string };
    return parsed.symbol ?? null;
  } catch {
    return null;
  }
}
