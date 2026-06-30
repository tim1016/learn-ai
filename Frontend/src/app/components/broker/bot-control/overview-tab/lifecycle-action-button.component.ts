import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  LifecycleChartAction,
  LifecycleChartActionId,
} from '../../../../api/live-instances.types';
import { actionHelp } from '../concept-help.registry';

@Component({
  selector: 'app-lifecycle-action-button',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lifecycle-action-button.component.html',
  styleUrl: './lifecycle-action-button.component.scss',
})
export class LifecycleActionButtonComponent {
  readonly action = input.required<LifecycleChartAction>();
  readonly busyAction = input<string | null>(null);
  readonly enabledCaption = input<string>('Available');

  readonly actionInvoked = output<LifecycleChartActionId>();
  readonly disabledActionSelected = output<string>();
  readonly actionTargetHovered = output<string | null>();

  readonly isBackendDisabled = computed(() => !this.action().enabled);
  readonly isInteractionLocked = computed(() => this.busyAction() !== null);
  readonly isDisabled = computed(() => this.isBackendDisabled() || this.isInteractionLocked());
  readonly statusHeadline = computed(() => {
    if (this.isBackendDisabled()) return this.action().reason_headline;
    if (this.isInteractionLocked()) return 'Request in flight';
    return this.enabledCaption();
  });
  readonly statusDetail = computed(() =>
    this.isBackendDisabled() ? this.action().reason_detail : null,
  );

  actionHelp(): string {
    return actionHelp(this.action().id);
  }

  activateAction(): void {
    const action = this.action();
    if (this.isDisabled()) {
      if (action.target_node_id) {
        this.disabledActionSelected.emit(action.target_node_id);
      }
      return;
    }
    this.actionInvoked.emit(action.id);
  }

  hoverAction(active: boolean): void {
    this.actionTargetHovered.emit(active ? this.action().target_node_id : null);
  }
}
