import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";
import { RouterLink } from "@angular/router";

import { RunReportComponent } from "../../engine-lab/run-report/run-report.component";

/** Read-only route container for one immutable persisted Strategy Lab run. */
@Component({
  selector: "app-strategy-lab-results",
  imports: [RouterLink, RunReportComponent],
  templateUrl: "./strategy-lab-results.component.html",
  styleUrl: "./strategy-lab-results.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'page-inset' },
})
export class StrategyLabResultsComponent {
  readonly id = input<string | null>(null);
  readonly runId = computed(() => parseRunId(this.id()));
  readonly restoreQuery = computed(() => {
    const runId = this.runId();
    return runId === null ? null : { restoreRun: runId };
  });
}

function parseRunId(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}
