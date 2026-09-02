import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { RunVerdict } from "../../../api/run-verdict.types";
import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { ResultsSidebarComponent } from "../results-sidebar/results-sidebar.component";
import { ResultsSummaryComponent } from "../results-summary/results-summary.component";
import { StrategyLabDeepDivesComponent } from "../strategy-lab-deep-dives/strategy-lab-deep-dives.component";
import type { StrategyLabParityView } from "../strategy-lab.models";

/** The workbench left column's results block, beneath the configuration. */
@Component({
  selector: "app-strategy-lab-run-stats",
  imports: [ResultsSummaryComponent, ResultsSidebarComponent, StrategyLabDeepDivesComponent],
  templateUrl: "./strategy-lab-run-stats.component.html",
  styleUrl: "./strategy-lab-run-stats.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabRunStatsComponent {
  readonly run = input.required<BacktestRunDetail>();
  readonly result = input.required<EngineResultData>();
  readonly verdict = input<RunVerdict | null>(null);
  readonly parity = input<StrategyLabParityView | null>(null);
  readonly tradesTruncated = input(false);
}
