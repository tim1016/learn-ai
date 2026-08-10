import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { LeanAnalysisFinding } from "../../../lean-engine/engine-results/engine-results.component";

@Component({
  selector: "app-strategy-lab-lean-analysis",
  templateUrl: "./lean-analysis.component.html",
  styleUrl: "./lean-analysis.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanAnalysisComponent {
  readonly findings = input.required<readonly LeanAnalysisFinding[]>();

  formatName(name: string): string {
    return name
      .replace(/Analysis$/, "")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2");
  }

  formatSample(sample: unknown): string {
    if (sample === null || sample === undefined) return "No sample supplied.";
    return typeof sample === "string" ? sample : JSON.stringify(sample, null, 2);
  }
}
