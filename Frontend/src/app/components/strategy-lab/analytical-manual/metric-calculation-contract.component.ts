import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import { KatexDirective } from "../../../shared/katex.directive";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import type { MetricVariant } from "./analytical-metric-catalog.models";

/** Trader-readable calculation contract; it never computes a metric. */
@Component({
  selector: "app-metric-calculation-contract",
  imports: [KatexDirective, ReceiptLabelPipe],
  templateUrl: "./metric-calculation-contract.component.html",
  styleUrl: "./metric-calculation-contract.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MetricCalculationContractComponent {
  readonly variant = input.required<MetricVariant>();
}
