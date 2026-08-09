import { ChangeDetectionStrategy, Component, computed, input, output, signal } from "@angular/core";

import type { RunVerdict } from "../../../api/run-verdict.types";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";

export interface StrategyLabParityView {
  status: string;
  reason: string | null;
  countsByCategory: { category: string; count: number }[];
  divergences: { category: string; message: string }[];
}

@Component({
  selector: "app-strategy-lab-verdict",
  imports: [ReceiptLabelPipe],
  templateUrl: "./strategy-lab-verdict.component.html",
  styleUrl: "./strategy-lab-verdict.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabVerdictComponent {
  readonly verdict = input<RunVerdict | null>(null);
  readonly parity = input<StrategyLabParityView | null>(null);
  readonly tradeCount = input(0);
  readonly rerun = output();
  readonly parityExpanded = signal(false);

  readonly band = computed<"green" | "amber" | "red" | "na">(() => {
    const score = this.verdict()?.composite;
    if (score === null || score === undefined) return "na";
    if (score >= 70) return "green";
    if (score >= 55) return "amber";
    return "red";
  });

  readonly grade = computed(() => {
    const verdict = this.verdict();
    if (!verdict?.grade || verdict.composite === null) return "Not graded";
    return `${verdict.grade} · ${Math.round(verdict.composite)}`;
  });

  toggleParity(): void {
    if (this.parity()?.status === "diverged") {
      this.parityExpanded.update((value) => !value);
    }
  }
}
