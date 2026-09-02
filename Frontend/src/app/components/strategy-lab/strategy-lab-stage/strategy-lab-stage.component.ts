import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import type { TradingMarker, TradingPoint } from "../../../shared/trading-chart";
import { ValidationStagePlaceholderComponent } from "../../lean-engine/validation-stage-placeholder/validation-stage-placeholder.component";
import { StrategyLabChartComponent } from "../strategy-lab-chart/strategy-lab-chart.component";

/**
 * The workbench's evidence column. A run in flight never destroys the chart it
 * is about to replace: the previous run stays mounted and is dimmed under a
 * progress overlay, so nothing is removed before its replacement exists and a
 * stale chart cannot read as live.
 */
@Component({
  selector: "app-strategy-lab-stage",
  imports: [ValidationStagePlaceholderComponent, StrategyLabChartComponent],
  templateUrl: "./strategy-lab-stage.component.html",
  styleUrl: "./strategy-lab-stage.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabStageComponent {
  readonly run = input<BacktestRunDetail | null>(null);
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly equityPoints = input<readonly TradingPoint[]>([]);
  readonly notices = input<readonly string[]>([]);
  readonly running = input(false);
  readonly runStatus = input("");
  readonly runPhaseDetail = input("");
  readonly symbol = input.required<string>();
  readonly resolution = input.required<string>();
  readonly fillMode = input.required<string>();
  readonly engine = input.required<string>();
  readonly dataPolicyNote = input("");
}
