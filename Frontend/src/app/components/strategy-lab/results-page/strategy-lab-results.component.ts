import { ChangeDetectionStrategy, Component, computed, input, signal } from "@angular/core";
import { RouterLink } from "@angular/router";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { RunReportComponent } from "../../engine-lab/run-report/run-report.component";
import { ResultsSidebarComponent } from "../results-sidebar/results-sidebar.component";

/** Read-only route container for one immutable persisted Strategy Lab run. */
@Component({
  selector: "app-strategy-lab-results",
  imports: [RouterLink, RunReportComponent, ResultsSidebarComponent],
  templateUrl: "./strategy-lab-results.component.html",
  styleUrl: "./strategy-lab-results.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabResultsComponent {
  readonly id = input<string | null>(null);
  readonly run = signal<BacktestRunDetail | null>(null);
  readonly runId = computed(() => parseRunId(this.id()));
  readonly restoreQuery = computed(() => {
    const runId = this.runId();
    return runId === null ? null : { restoreRun: runId };
  });

  acceptRun(run: BacktestRunDetail): void {
    if (run.id === this.runId()) this.run.set(run);
  }
}

function parseRunId(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}
