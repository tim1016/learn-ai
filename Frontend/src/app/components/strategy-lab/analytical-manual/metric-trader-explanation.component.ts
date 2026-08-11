import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { MetricVariant } from "./analytical-metric-catalog.models";

/** Trader-first meaning and cautions, authored by the Python catalog. */
@Component({
  selector: "app-metric-trader-explanation",
  templateUrl: "./metric-trader-explanation.component.html",
  styleUrl: "./metric-trader-explanation.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MetricTraderExplanationComponent {
  readonly variant = input.required<MetricVariant>();
}
