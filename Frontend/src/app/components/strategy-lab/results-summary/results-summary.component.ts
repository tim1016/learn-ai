import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type { RunVerdict } from "../../../api/run-verdict.types";
import type { MetricDocumentationContext } from "../../../graphql/backtest-runs.query";
import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { STRATEGY_METRIC_HELP } from "../../lean-engine/metric-grade.util";
import { EvidenceGradeComponent } from "../evidence-grade/evidence-grade.component";
import { MetricHelpPopoverComponent } from "../metric-help-popover/metric-help-popover.component";

/** Headline KPI row for one persisted Strategy Lab run. */
@Component({
  selector: "app-strategy-lab-results-summary",
  imports: [EvidenceGradeComponent, MetricHelpPopoverComponent],
  templateUrl: "./results-summary.component.html",
  styleUrl: "./results-summary.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResultsSummaryComponent {
  readonly result = input.required<EngineResultData>();
  readonly verdict = input<RunVerdict | null>(null);
  readonly metricDocumentation = input<MetricDocumentationContext[]>([]);
  readonly runId = input<number | null>(null);
  readonly help = STRATEGY_METRIC_HELP;

  readonly sharpeDocumentation = computed(
    () => this.metricDocumentation().find((context) => context.metricId === "sharpe") ?? null,
  );
  readonly sortinoDocumentation = computed(
    () => this.metricDocumentation().find((context) => context.metricId === "sortino") ?? null,
  );
  readonly maxDrawdownDocumentation = computed(
    () => this.metricDocumentation().find((context) => context.metricId === "maximum_drawdown") ?? null,
  );
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
