import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type { RunVerdictSubScore } from "../../../api/run-verdict.types";
import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { parseRunVerdict } from "../strategy-lab.models";

type EvidenceBand = "positive" | "warning" | "negative";

interface SidebarMetric extends RunVerdictSubScore {
  band: EvidenceBand;
}

const HEADLINE_METRIC_KEYS = new Set([
  "sharpe",
  "sortino",
  "max_dd",
  "pf",
  "win_rate",
  "sample",
]);

@Component({
  selector: "app-strategy-lab-results-sidebar",
  imports: [ReceiptLabelPipe],
  templateUrl: "./results-sidebar.component.html",
  styleUrl: "./results-sidebar.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResultsSidebarComponent {
  readonly run = input.required<BacktestRunDetail>();

  readonly metrics = computed<SidebarMetric[]>(() => {
    const verdict = parseRunVerdict(this.run().verdictJson);
    if (!verdict) return [];
    return verdict.dimensions.flatMap((dimension) =>
      dimension.sub_scores
        .filter(isSupplementaryScoredMetric)
        .map((metric) => ({ ...metric, band: scoreBand(metric.score) })),
    );
  });

  readonly parityStatus = computed(() => {
    const latest = [...this.run().parityVerdicts]
      .sort((left, right) => right.createdAt - left.createdAt)[0];
    return latest?.status ?? null;
  });

  readonly parityBand = computed<EvidenceBand>(() => {
    const status = this.parityStatus()?.toLowerCase();
    if (status === "complete" || status === "equivalent" || status === "within_tolerance") {
      return "positive";
    }
    return status === "pending" ? "warning" : "negative";
  });
}

function isSupplementaryScoredMetric(
  metric: RunVerdictSubScore,
): metric is RunVerdictSubScore & { score: number } {
  return metric.score !== null && !HEADLINE_METRIC_KEYS.has(metric.key);
}

function scoreBand(score: number): EvidenceBand {
  if (score >= 15) return "positive";
  if (score >= 8) return "warning";
  return "negative";
}
