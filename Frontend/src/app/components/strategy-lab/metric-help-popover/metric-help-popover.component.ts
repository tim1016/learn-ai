import { ChangeDetectionStrategy, Component, computed, input, signal } from "@angular/core";
import { RouterLink } from "@angular/router";

import type { MetricDocumentationContext } from "../../../graphql/backtest-runs.query";
import type { MetricHelp } from "../../lean-engine/metric-grade.util";

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
    if (!context) return { metric: this.metric().id };
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
