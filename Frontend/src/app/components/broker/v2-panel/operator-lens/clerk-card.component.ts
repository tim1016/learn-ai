import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import type { ClerkCard, PanelAction } from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * Account/clerk card (spec §7.3).
 *
 * Hold state, reconciliation verdict + last sweep, outstanding uncertain
 * intents, channel health.
 *
 * Actions: Reconcile now and Clear hold.
 * - Clear hold: enabled only when root condition is healthy AND freshly observed;
 *   confirmation shows the account-wide blast radius (§7.3).
 * - No force-override path (spec decision #20).
 *
 * <!-- card-help anchor: holds — wired to S5 drawer in end-phase integration -->
 * <!-- card-help anchor: reconciliation — wired to S5 drawer in end-phase integration -->
 */
@Component({
  selector: 'app-clerk-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './clerk-card.component.html',
  styleUrl: './clerk-card.component.scss',
})
export class ClerkCardComponent {
  readonly clerk = input.required<ClerkCard>();
  readonly reconcileAction = input<PanelAction | null>(null);
  readonly clearHoldAction = input<PanelAction | null>(null);
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelAction>();

  protected readonly reconcileEnabled = computed(
    () => this.reconcileAction()?.enabled ?? false,
  );

  protected readonly clearHoldEnabled = computed(
    () => this.clearHoldAction()?.enabled ?? false,
  );

  protected readonly reconcileBlockers = computed(
    () => this.reconcileAction()?.blockers ?? [],
  );

  protected readonly clearHoldBlockers = computed(
    () => this.clearHoldAction()?.blockers ?? [],
  );

  protected readonly clearHoldConfirmation = computed(
    () => this.clearHoldAction()?.confirmation ?? null,
  );

  protected onReconcile(): void {
    const action = this.reconcileAction();
    if (action && action.enabled) {
      this.actionRequested.emit(action);
    }
  }

  protected onClearHold(): void {
    const action = this.clearHoldAction();
    if (!action || !action.enabled) return;
    // If a confirmation is required, the template handles it inline (see template).
    this.actionRequested.emit(action);
  }

  protected trackChannel(_index: number, channel: { stream: string }): string {
    return channel.stream;
  }
}
