import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { StrategyInfo } from '../lean-engine.component';

/**
 * Read-only detail view for a single registered strategy, hosted in its own
 * Engine Lab tab. Emits navigation intents rather than mutating parent state
 * directly, so the workbench owns tab/selection wiring.
 */
@Component({
  selector: 'app-strategy-detail-tab',
  templateUrl: './strategy-detail-tab.component.html',
  styleUrl: './strategy-detail-tab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyDetailTabComponent {
  readonly strategy = input.required<StrategyInfo>();

  /** Close this detail tab (removes it and returns to the workbench). */
  readonly closed = output();
  /** Jump back to the workbench configuration for this strategy. */
  readonly configure = output();
}
