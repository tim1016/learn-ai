import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { STRATEGY_METRIC_HELP } from "../../lean-engine/metric-grade.util";
import { MetricHelpPopoverComponent } from "../metric-help-popover/metric-help-popover.component";

@Component({
  selector: "app-strategy-lab-metrics",
  imports: [MetricHelpPopoverComponent],
  templateUrl: "./strategy-lab-metrics.component.html",
  styleUrl: "./strategy-lab-metrics.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabMetricsComponent {
  readonly result = input.required<EngineResultData>();
  readonly help = STRATEGY_METRIC_HELP;

  formatCurrency(value: number | null | undefined): string {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    }).format(value);
  }

  formatNumber(value: number | null | undefined): string {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    if (!Number.isFinite(value)) return "∞";
    return value.toFixed(2);
  }

  formatPercent(value: number | null | undefined): string {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return `${(value * 100).toFixed(2)}%`;
  }
}
