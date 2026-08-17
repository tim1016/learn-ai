import { ChangeDetectionStrategy, Component, computed, input, signal } from "@angular/core";
import { Drawer } from "primeng/drawer";

import type {
  EngineResultData,
  LeanAnalysisFinding,
} from "../../lean-engine/engine-results/engine-results.component";
import { LeanStatisticsComponent } from "../../lean-engine/lean-statistics/lean-statistics.component";
import { TradeLedgerComponent } from "../../lean-engine/engine-results/trade-ledger/trade-ledger.component";
import { ValidationAtlasComponent } from "../../lean-engine/engine-results/validation-atlas/validation-atlas.component";
import type { StrategyLabParityView } from "../strategy-lab.models";
import { CompatibilityEvidenceComponent } from "./compatibility-evidence/compatibility-evidence.component";
import { LeanAnalysisComponent } from "./lean-analysis/lean-analysis.component";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { SharpePnlDivergenceComponent } from "./sharpe-pnl-divergence/sharpe-pnl-divergence.component";

type EvidenceSectionId = "compatibility" | "statistics" | "analysis" | "divergence" | "atlas" | "ledger";
type EvidenceSectionSummary =
  | { kind: "copy"; value: string }
  | { kind: "receipt"; value: string };

interface EvidenceSection {
  id: EvidenceSectionId;
  label: string;
  summary: EvidenceSectionSummary;
}

@Component({
  selector: "app-strategy-lab-deep-dives",
  imports: [
    Drawer,
    CompatibilityEvidenceComponent,
    LeanAnalysisComponent,
    LeanStatisticsComponent,
    ReceiptLabelPipe,
    SharpePnlDivergenceComponent,
    TradeLedgerComponent,
    ValidationAtlasComponent,
  ],
  templateUrl: "./strategy-lab-deep-dives.component.html",
  styleUrl: "./strategy-lab-deep-dives.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabDeepDivesComponent {
  readonly result = input.required<EngineResultData>();
  readonly tradesTruncated = input(false);
  readonly parity = input<StrategyLabParityView | null>(null);

  private readonly selectedId = signal<EvidenceSectionId | null>(null);

  readonly leanAnalysis = computed<LeanAnalysisFinding[]>(() => this.result().lean_analysis ?? []);
  readonly leanStatistics = computed(() => {
    const statistics = this.result().lean_statistics;
    return statistics?.portfolio && statistics.trade && statistics.runtime ? statistics : null;
  });
  readonly sections = computed<EvidenceSection[]>(() => {
    const result = this.result();
    const sections: EvidenceSection[] = [];
    const parity = this.parity();
    if (parity) {
      sections.push({
        id: "compatibility",
        label: "Compatibility evidence",
        summary: { kind: "receipt", value: parity.status },
      });
    }
    if (this.leanStatistics()) {
      sections.push({
        id: "statistics",
        label: "Native LEAN statistics",
        summary: { kind: "copy", value: "Complete engine-authored statistics catalog" },
      });
    }
    const analysis = this.leanAnalysis();
    if (analysis.length > 0) {
      sections.push({
        id: "analysis",
        label: "LEAN native analysis",
        summary: {
          kind: "copy",
          value: `${analysis.length} Oracle finding${analysis.length === 1 ? "" : "s"} with complete evidence`,
        },
      });
    }
    const divergence = result.validation_analytics?.sharpe_pnl_divergence;
    if (divergence) {
      sections.push({
        id: "divergence",
        label: "Sharpe–P&L divergence",
        summary: {
          kind: "copy",
          value: divergence.error
            ? "Native equity study unavailable"
            : `${divergence.return_window_sessions}-session Sharpe versus cumulative P&L`,
        },
      });
    }
    sections.push(
      {
        id: "atlas",
        label: "Validation atlas",
        summary: { kind: "copy", value: "Horizons, timing, seasonality, stability" },
      },
      {
        id: "ledger",
        label: "Trade ledger",
        summary: {
          kind: "copy",
          value: this.tradesTruncated()
            ? `${result.trades.length} recent of ${result.total_trades} completed trades`
            : `${result.total_trades} completed trades`,
        },
      },
    );
    return sections;
  });
  readonly selectedSection = computed(() => {
    const id = this.selectedId();
    return id === null ? null : this.sections().find((section) => section.id === id) ?? null;
  });

  open(section: EvidenceSection): void {
    this.selectedId.set(section.id);
  }

  onDrawerVisibilityChange(visible: boolean): void {
    if (!visible) this.selectedId.set(null);
  }
}
