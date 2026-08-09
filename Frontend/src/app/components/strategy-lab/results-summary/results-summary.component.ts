import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type { RunVerdict } from "../../../api/run-verdict.types";
import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { STRATEGY_METRIC_HELP } from "../../lean-engine/metric-grade.util";
import { MetricHelpPopoverComponent } from "../metric-help-popover/metric-help-popover.component";

/** Headline KPI row for one persisted Strategy Lab run. */
@Component({
  selector: "app-strategy-lab-results-summary",
  imports: [MetricHelpPopoverComponent],
  templateUrl: "./results-summary.component.html",
  styleUrl: "./results-summary.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResultsSummaryComponent {
  readonly result = input.required<EngineResultData>();
  readonly verdict = input<RunVerdict | null>(null);
  readonly help = STRATEGY_METRIC_HELP;

  readonly grade = computed(() => this.verdict()?.grade ?? "—");
  readonly score = computed(() => {
    const composite = this.verdict()?.composite;
    return composite === null || composite === undefined ? "Not scored" : `${Math.round(composite)} / 100`;
  });
  readonly gradeBand = computed<"positive" | "warning" | "negative" | "neutral">(() => {
    const grade = this.verdict()?.grade?.toUpperCase();
    if (grade === "A" || grade === "A+") return "positive";
    if (grade === "B" || grade === "B+") return "warning";
    if (grade === "C" || grade === "D" || grade === "F") return "negative";
    return "neutral";
  });

  currency(value: number | null | undefined): string {
    if (!isFiniteNumber(value)) return "—";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    }).format(value);
  }

  number(value: number | null | undefined): string {
    return isFiniteNumber(value) ? value.toFixed(2) : "—";
  }

  percent(value: number | null | undefined): string {
    return isFiniteNumber(value) ? `${(value * 100).toFixed(2)}%` : "—";
  }
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value);
}
