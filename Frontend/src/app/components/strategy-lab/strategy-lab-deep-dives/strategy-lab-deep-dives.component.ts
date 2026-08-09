import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type {
  EngineResultData,
  LeanAnalysisFinding,
  LeanStatistics,
} from "../../lean-engine/engine-results/engine-results.component";
import { TradeLedgerComponent } from "../../lean-engine/engine-results/trade-ledger/trade-ledger.component";
import { ValidationAtlasComponent } from "../../lean-engine/engine-results/validation-atlas/validation-atlas.component";
import { LeanStatisticsComponent } from "../../lean-engine/lean-statistics/lean-statistics.component";

@Component({
  selector: "app-strategy-lab-deep-dives",
  imports: [LeanStatisticsComponent, TradeLedgerComponent, ValidationAtlasComponent],
  templateUrl: "./strategy-lab-deep-dives.component.html",
  styleUrl: "./strategy-lab-deep-dives.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabDeepDivesComponent {
  readonly result = input.required<EngineResultData>();
  readonly commissionPerOrder = input<number | null>(null);

  readonly leanStats = computed<LeanStatistics | null>(() => {
    const stats = this.result().lean_statistics;
    return stats?.portfolio && stats.trade && stats.runtime ? stats : null;
  });

  readonly leanAnalysis = computed<LeanAnalysisFinding[]>(() =>
    this.result().lean_analysis ?? [],
  );

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

  formatCurrency(value: number | null | undefined): string {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    }).format(value);
  }
}
