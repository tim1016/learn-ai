import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";
import { CurrencyPipe } from "@angular/common";
import { EngineSourceLiteral, RunHistoryRow } from "./run-history.types";

const ENGINE_LABELS: Record<EngineSourceLiteral, string> = {
  engine: "Engine Lab",
  "strategy-lab": "Strategy Lab",
  "lean-sidecar": "LEAN",
};

@Component({
  selector: "app-run-history",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe],
  templateUrl: "./run-history.component.html",
  styleUrl: "./run-history.component.scss",
})
export class RunHistoryComponent {
  readonly rows = input.required<RunHistoryRow[]>();

  readonly isEmpty = computed(() => this.rows().length === 0);

  badge(source: EngineSourceLiteral): string {
    return ENGINE_LABELS[source];
  }
}
