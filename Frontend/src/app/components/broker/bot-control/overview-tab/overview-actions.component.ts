import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  LifecycleChartAction,
  LifecycleChartActionId,
} from '../../../../api/live-instances.types';
import { LifecycleActionButtonComponent } from './lifecycle-action-button.component';

type ToolbarGroupId = 'run' | 'recover' | 'danger';
type ToolbarActionId = Extract<
  LifecycleChartActionId,
  'start_process' | 'resume' | 'pause' | 'flatten_and_pause' | 'stop' | 'redeploy' | 'mark_poisoned'
>;

interface ToolbarGroupDefinition {
  readonly id: ToolbarGroupId;
  readonly label: string;
  readonly ariaLabel: string;
  readonly actionIds: readonly ToolbarActionId[];
}

interface ToolbarGroup {
  readonly id: ToolbarGroupId;
  readonly label: string;
  readonly ariaLabel: string;
  readonly actions: readonly LifecycleChartAction[];
}

const TOOLBAR_GROUPS: readonly ToolbarGroupDefinition[] = [
  {
    id: 'run',
    label: 'Run',
    ariaLabel: 'Run lifecycle controls',
    actionIds: ['start_process', 'resume', 'pause'],
  },
  {
    id: 'recover',
    label: 'Recover',
    ariaLabel: 'Recovery lifecycle controls',
    actionIds: ['flatten_and_pause', 'stop', 'redeploy'],
  },
  {
    id: 'danger',
    label: 'Danger',
    ariaLabel: 'Danger lifecycle controls',
    actionIds: ['mark_poisoned'],
  },
];

@Component({
  selector: 'app-overview-actions',
  imports: [LifecycleActionButtonComponent],
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
  readonly onlyFreshRunAvailable = computed(() => {
    const enabledActions = this.actions().filter((action) => action.enabled);
    return enabledActions.length === 1 && enabledActions[0].id === 'redeploy';
  });
  readonly actionGroups = computed<readonly ToolbarGroup[]>(() => {
    const byId = this.actionsById();
    return TOOLBAR_GROUPS
      .map((group) => ({
        id: group.id,
        label: group.label,
        ariaLabel: group.ariaLabel,
        actions: group.actionIds
          .map((id) => byId.get(id))
          .filter((action): action is LifecycleChartAction => action !== undefined),
      }))
      .filter((group) => group.actions.length > 0);
  });

  trackAction(_: number, action: LifecycleChartAction): LifecycleChartActionId {
    return action.id;
  }

  trackGroup(_: number, group: ToolbarGroup): ToolbarGroupId {
    return group.id;
  }
}
