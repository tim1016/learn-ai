import { ChangeDetectionStrategy, Component, input, signal } from "@angular/core";

import type { MetricHelp } from "../../lean-engine/metric-grade.util";

@Component({
  selector: "app-metric-help-popover",
  templateUrl: "./metric-help-popover.component.html",
  styleUrl: "./metric-help-popover.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    "(keydown.escape)": "close()",
  },
})
export class MetricHelpPopoverComponent {
  readonly metric = input.required<MetricHelp>();
  readonly open = signal(false);

  toggle(): void {
    this.open.update((value) => !value);
  }

  close(): void {
    this.open.set(false);
  }
}
