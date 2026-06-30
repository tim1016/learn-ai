import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  LifecycleChartAction,
  LifecycleChartActionId,
} from '../../../../api/live-instances.types';
import { LifecycleActionButtonComponent } from './lifecycle-action-button.component';

type EmergencyActionId = Extract<
  LifecycleChartActionId,
  'start_process' | 'resume' | 'pause' | 'flatten_and_pause' | 'stop'
>;

const EMERGENCY_ACTION_ORDER: readonly EmergencyActionId[] = [
  'start_process',
  'resume',
  'pause',
  'flatten_and_pause',
  'stop',
];

@Component({
  selector: 'app-overview-actions',
  imports: [CommonModule, LifecycleActionButtonComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './overview-actions.component.html',
  styleUrl: './overview-actions.component.scss',
})
export class OverviewActionsComponent {
  readonly actions = input.required<LifecycleChartAction[]>();
  readonly busyAction = input<string | null>(null);
  readonly actionInvoked = output<LifecycleChartActionId>();
  readonly disabledActionSelected = output<string>();
  readonly actionTargetHovered = output<string | null>();

  readonly emergencyActions = computed(() => {
    const byId = this.actionsById();
    return EMERGENCY_ACTION_ORDER
      .map((id) => byId.get(id))
      .filter((action): action is LifecycleChartAction => action !== undefined);
  });
  readonly redeployAction = computed(() => {
    const action = this.actionsById().get('redeploy');
    return action ?? null;
  });
  readonly overflowActions = computed(() => {
    const action = this.actionsById().get('mark_poisoned');
    return action ? [action] : [];
  });

  trackAction(_: number, action: LifecycleChartAction): LifecycleChartActionId {
    return action.id;
  }

  private actionsById(): Map<LifecycleChartActionId, LifecycleChartAction> {
    return new Map(this.actions().map((action) => [action.id, action]));
  }
}
