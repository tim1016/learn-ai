import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  LifecycleChartAction,
  LifecycleChartActionId,
} from '../../../../api/live-instances.types';

const ACTION_ICON: Record<LifecycleChartActionId, string> = {
  start_process: 'pi pi-play',
  resume: 'pi pi-play',
  pause: 'pi pi-pause',
  flatten_and_pause: 'pi pi-stop-circle',
  stop: 'pi pi-power-off',
  mark_poisoned: 'pi pi-ban',
  redeploy: 'pi pi-refresh',
};

const ACTION_LABEL: Partial<Record<LifecycleChartActionId, string>> = {
  redeploy: 'Fresh run',
};

@Component({
  selector: 'app-lifecycle-action-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lifecycle-action-button.component.html',
  styleUrl: './lifecycle-action-button.component.scss',
})
export class LifecycleActionButtonComponent {
  readonly action = input.required<LifecycleChartAction>();
  readonly busyAction = input<string | null>(null);

  readonly actionInvoked = output<LifecycleChartActionId>();
  readonly disabledActionSelected = output<string>();
  readonly actionTargetHovered = output<string | null>();

  readonly isBackendDisabled = computed(() => !this.action().enabled);
  readonly isInteractionLocked = computed(() => this.busyAction() !== null);
  readonly isDisabled = computed(() => this.isBackendDisabled() || this.isInteractionLocked());
  readonly statusHeadline = computed(() => {
    if (this.isInteractionLocked()) return 'Request in flight';
    return this.action().reason_headline;
  });
  readonly statusDetail = computed(() =>
    this.isInteractionLocked() ? null : this.action().reason_detail,
  );
  readonly displayLabel = computed(() => ACTION_LABEL[this.action().id] ?? this.action().label);
  readonly iconClass = computed(() =>
    this.busyAction() === this.action().id ? 'pi pi-spinner pi-spin' : ACTION_ICON[this.action().id],
  );
  readonly disabledTooltip = computed(() => {
    if (!this.isDisabled()) return null;
    const detail = this.statusDetail();
    return detail ? `${this.statusHeadline()}. ${detail}` : this.statusHeadline();
  });

  ariaLabel(): string {
    return this.displayLabel();
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
