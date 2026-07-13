import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";
import { RouterLink } from "@angular/router";

import { RunReportComponent } from "../run-report/run-report.component";

/** Route wrapper for /engine/runs/:id — the report itself is the shared
 *  RunReportComponent, the exact tree the workbench renders after a run. */
@Component({
  selector: "app-engine-run-detail",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RunReportComponent],
  template: `
    <main class="run-detail">
      <a class="back-link" routerLink="/engine">Back to Engine Lab</a>
      @if (runId(); as id) {
        <app-engine-run-report [runId]="id" />
      } @else {
        <section class="empty-state">Run not found.</section>
      }
    </main>
  `,
  styleUrls: ["./engine-run-detail.component.scss"],
})
export class EngineRunDetailComponent {
  readonly id = input<string | null>(null);

  readonly runId = computed<number | null>(() => {
    const parsed = Number(this.id());
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  });
}
