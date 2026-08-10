import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import { ReceiptLabelPipe } from "../../../../shared/pipes/receipt-label.pipe";
import type { StrategyLabParityView } from "../../strategy-lab.models";

@Component({
  selector: "app-strategy-lab-compatibility-evidence",
  imports: [ReceiptLabelPipe],
  templateUrl: "./compatibility-evidence.component.html",
  styleUrl: "./compatibility-evidence.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CompatibilityEvidenceComponent {
  readonly parity = input.required<StrategyLabParityView>();
}
