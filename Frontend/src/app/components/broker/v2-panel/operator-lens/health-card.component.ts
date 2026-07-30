import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import type { BotHealthCard, PanelAction } from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * Bot health card (spec §7.2).
 *
 * Phase, desired state, duty outcome (kind + backend reason), decision-receipt
 * freshness, last bar seen, and the terminal Retire action.
 *
 * <!-- card-help anchor: bot-lifecycle — wired to S5 drawer in end-phase integration -->
 */
@Component({
  selector: 'app-health-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './health-card.component.html',
  styleUrl: './health-card.component.scss',
})
export class HealthCardComponent {
  readonly health = input.required<BotHealthCard>();
  /** The retire action from the presented-actions list, or null if not presented. */
  readonly retireAction = input<PanelAction | null>(null);
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelAction>();

  protected readonly canRetire = computed(() => {
    const a = this.retireAction();
    return a !== null && a.enabled;
  });

  protected readonly retireBlockers = computed(
    () => this.retireAction()?.blockers ?? [],
  );

  protected onRetire(): void {
    const action = this.retireAction();
    if (action && action.enabled) {
      this.actionRequested.emit(action);
    }
  }
}
