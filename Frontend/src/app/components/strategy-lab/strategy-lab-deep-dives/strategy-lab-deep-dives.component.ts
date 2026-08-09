import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type {
  EngineResultData,
  LeanAnalysisFinding,
} from "../../lean-engine/engine-results/engine-results.component";
import { LeanStatisticsComponent } from "../../lean-engine/lean-statistics/lean-statistics.component";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TradeLedgerComponent } from "../../lean-engine/engine-results/trade-ledger/trade-ledger.component";
import { ValidationAtlasComponent } from "../../lean-engine/engine-results/validation-atlas/validation-atlas.component";
import type { StrategyLabParityView } from "../strategy-lab.models";

@Component({
  selector: "app-strategy-lab-deep-dives",
  imports: [TradeLedgerComponent, ValidationAtlasComponent, LeanStatisticsComponent, ReceiptLabelPipe],
  templateUrl: "./strategy-lab-deep-dives.component.html",
  styleUrl: "./strategy-lab-deep-dives.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabDeepDivesComponent {
  readonly result = input.required<EngineResultData>();
  readonly tradesTruncated = input(false);
  readonly parity = input<StrategyLabParityView | null>(null);

  readonly leanAnalysis = computed<LeanAnalysisFinding[]>(() =>
    this.result().lean_analysis ?? [],
  );
  readonly leanStatistics = computed(() => {
    const statistics = this.result().lean_statistics;
    return statistics?.portfolio && statistics.trade && statistics.runtime ? statistics : null;
  });

  formatLeanAnalysisName(name: string): string {
    return name
      .replace(/Analysis$/, "")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2");
  }

  formatLeanSample(sample: unknown): string {
    if (sample === null || sample === undefined) return "No sample supplied.";
    if (typeof sample === "string") return sample;
    return JSON.stringify(sample, null, 2);
  }

}
