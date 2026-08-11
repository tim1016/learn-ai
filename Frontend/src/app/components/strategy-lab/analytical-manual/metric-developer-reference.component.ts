import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import type { MetricVariant } from "./analytical-metric-catalog.models";

/** Exact implementation and validation receipts for maintainers and reviewers. */
@Component({
  selector: "app-metric-developer-reference",
  imports: [ReceiptLabelPipe],
  templateUrl: "./metric-developer-reference.component.html",
  styleUrl: "./metric-developer-reference.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MetricDeveloperReferenceComponent {
  readonly variant = input.required<MetricVariant>();
}
