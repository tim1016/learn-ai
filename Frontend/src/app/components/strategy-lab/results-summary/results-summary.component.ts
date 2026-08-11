import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type { RunVerdict } from "../../../api/run-verdict.types";
import type { MetricDocumentationContext } from "../../../graphql/backtest-runs.query";
import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { EvidenceGradeComponent } from "../evidence-grade/evidence-grade.component";
import { MetricHelpModalComponent } from "../metric-help-modal/metric-help-modal.component";

/** Headline KPI row for one persisted Strategy Lab run. */
@Component({
  selector: "app-strategy-lab-results-summary",
  imports: [EvidenceGradeComponent, MetricHelpModalComponent],
  templateUrl: "./results-summary.component.html",
  styleUrl: "./results-summary.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResultsSummaryComponent {
  readonly result = input.required<EngineResultData>();
  readonly verdict = input<RunVerdict | null>(null);
  readonly metricDocumentation = input<MetricDocumentationContext[]>([]);
  readonly runId = input<number | null>(null);
  private readonly documentationByMetric = computed(() =>
    new Map(this.metricDocumentation().map((context) => [context.metricId, context])),
  );

  documentationFor(metricId: string): MetricDocumentationContext | null {
    return this.documentationByMetric().get(metricId) ?? null;
  }
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
