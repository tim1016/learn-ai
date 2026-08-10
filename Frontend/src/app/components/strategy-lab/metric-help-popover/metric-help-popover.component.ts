import { ChangeDetectionStrategy, Component, computed, input, signal } from "@angular/core";
import { RouterLink } from "@angular/router";

import type { MetricDocumentationContext } from "../../../graphql/backtest-runs.query";
import type { MetricHelp, StrategyMetricId } from "../../lean-engine/metric-grade.util";

// STRATEGY_METRIC_HELP.id is a legacy kebab-case identifier (e.g. "net-profit");
// the analytical metric catalog uses different stable ids (e.g. "net_pnl"). This
// maps every legacy id to its catalog metric_id so an unrecorded-context link
// still resolves to the requested metric's definition instead of catalog's
// fallback (Sharpe) entry.
const CATALOG_METRIC_ID: Readonly<Record<StrategyMetricId, string>> = {
  "net-profit": "net_pnl",
  "profit-factor": "profit_factor",
  expectancy: "expectancy",
  sharpe: "sharpe",
  sortino: "sortino",
  "max-drawdown": "maximum_drawdown",
  "win-rate": "win_rate",
  trades: "completed_trades",
};

@Component({
  selector: "app-metric-help-popover",
  imports: [RouterLink],
  templateUrl: "./metric-help-popover.component.html",
  styleUrl: "./metric-help-popover.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    "(keydown.escape)": "close()",
  },
})
export class MetricHelpPopoverComponent {
  readonly metric = input.required<MetricHelp>();
  readonly context = input<MetricDocumentationContext | null>(null);
  readonly runId = input<number | null>(null);
  readonly open = signal(false);
  readonly docsQuery = computed(() => {
    const context = this.context();
    if (!context) return { metric: CATALOG_METRIC_ID[this.metric().id] };
    return {
      metric: context.metricId,
      variant: context.variantId,
      producer: context.producer,
      contract: context.contractId ?? undefined,
      run: this.runId() ?? undefined,
    };
  });

  toggle(): void {
    this.open.update((value) => !value);
  }

  close(): void {
    this.open.set(false);
  }
}
