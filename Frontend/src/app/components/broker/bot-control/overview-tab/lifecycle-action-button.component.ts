import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  BotLifecycleAction,
  BotLifecycleActionId,
} from '../../../../api/live-instances.types';

const ACTION_ICON: Record<BotLifecycleActionId, string> = {
  confirm_start: 'pi pi-play',
  end_day_now: 'pi pi-power-off',
  retire_replace: 'pi pi-refresh',
  add_to_roster: 'pi pi-calendar-plus',
  take_off_roster: 'pi pi-calendar-minus',
};

@Component({
  selector: 'app-lifecycle-action-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lifecycle-action-button.component.html',
  styleUrl: './lifecycle-action-button.component.scss',
})
export class LifecycleActionButtonComponent {
  readonly action = input.required<BotLifecycleAction>();
  readonly busyAction = input<string | null>(null);

  readonly actionInvoked = output<BotLifecycleActionId>();
  readonly actionTargetHovered = output<string | null>();

  readonly isBackendDisabled = computed(() => !this.action().enabled);
  readonly isInteractionLocked = computed(() => this.busyAction() !== null);
  readonly isDisabled = computed(() => this.isBackendDisabled() || this.isInteractionLocked());
  readonly statusHeadline = computed(() => {
    if (this.isInteractionLocked()) return 'Request in flight';
    return this.action().enabled ? 'Available' : (this.action().reason ?? 'Unavailable');
  });
  readonly statusDetail = computed(() => null);
  readonly displayLabel = computed(() => this.action().label);
  readonly actionStateLabel = computed(() => this.isDisabled() ? 'Off' : 'On');
  readonly isOn = computed(() => !this.isDisabled());
  readonly isOff = computed(() => this.isDisabled());
  readonly isPrimaryTone = computed(() => this.action().id === 'confirm_start');
  readonly isDangerTone = computed(() => this.action().id === 'retire_replace');
  readonly iconClass = computed(() =>
    this.busyAction() === this.action().id ? 'pi pi-spinner pi-spin' : ACTION_ICON[this.action().id],
  );
  readonly tooltip = computed(() => {
    const detail = this.statusDetail();
    const status = detail ? `${this.statusHeadline()}. ${detail}` : this.statusHeadline();
    return `${this.displayLabel()} ${this.actionStateLabel()}. ${status}`;
  });

  activateAction(): void {
    if (this.isDisabled()) {
      return;
    }
    const action = this.action();
    this.actionInvoked.emit(action.id);
  }

  hoverAction(active: boolean): void {
    this.actionTargetHovered.emit(active ? this.action().id : null);
  }
}
