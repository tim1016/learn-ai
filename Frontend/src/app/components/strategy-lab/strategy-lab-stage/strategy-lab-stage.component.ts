import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { FillModeName } from "../../../models/fill-mode";
import type { BacktestRunDetail } from "../../../services/backtest-runs.types";
import type { TradingMarker, TradingPoint } from "../../../shared/trading-chart";
import { ValidationStagePlaceholderComponent } from "../../lean-engine/validation-stage-placeholder/validation-stage-placeholder.component";
import { StrategyLabStrategyViewComponent } from "../strategy-lab-strategy-view/strategy-lab-strategy-view.component";

/**
 * The workbench's evidence column: the run's strategy view, as the bot page
 * shows a bot's (#2639 D13), with its price-and-trades chart one tab away. A
 * run in flight never destroys the chart it is about to replace: the previous
 * run stays mounted and is dimmed under a progress overlay, so nothing is
 * removed before its replacement exists and a stale chart cannot read as live.
 */
@Component({
  selector: "app-strategy-lab-stage",
  imports: [ValidationStagePlaceholderComponent, StrategyLabStrategyViewComponent],
  templateUrl: "./strategy-lab-stage.component.html",
  styleUrl: "./strategy-lab-stage.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabStageComponent {
  readonly run = input<BacktestRunDetail | null>(null);
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly equityPoints = input<readonly TradingPoint[]>([]);
  readonly notices = input<readonly string[]>([]);
  readonly error = input<string | null>(null);
  readonly running = input(false);
  readonly runStatus = input("");
  readonly runPhaseDetail = input("");
  readonly symbol = input.required<string>();
  readonly resolution = input.required<string>();
  readonly fillMode = input.required<FillModeName>();
  readonly engine = input.required<string>();
  readonly dataPolicyNote = input("");
}
