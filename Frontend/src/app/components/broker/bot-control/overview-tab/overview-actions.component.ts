import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import type { MenuItem } from 'primeng/api';
import { Menu } from 'primeng/menu';

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

interface OverflowMenuItem extends MenuItem {
  readonly action: LifecycleChartAction;
}

@Component({
  selector: 'app-overview-actions',
  imports: [LifecycleActionButtonComponent, Menu],
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

  readonly actionsById = computed(() => new Map(this.actions().map((action) => [action.id, action])));
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
  readonly overflowMenuItems = computed<OverflowMenuItem[]>(() =>
    this.overflowActions().map((action) => ({
      label: action.label,
      action,
    })),
  );

  trackAction(_: number, action: LifecycleChartAction): LifecycleChartActionId {
    return action.id;
  }
}
