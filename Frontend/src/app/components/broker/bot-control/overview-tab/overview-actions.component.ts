import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  BotLifecycleAction,
  BotLifecycleActionId,
} from '../../../../api/live-instances.types';
import { LifecycleActionButtonComponent } from './lifecycle-action-button.component';

type ToolbarGroupId = 'duty' | 'roster' | 'machinery';

interface ToolbarGroupDefinition {
  readonly id: ToolbarGroupId;
  readonly label: string;
  readonly ariaLabel: string;
  readonly actionIds: readonly BotLifecycleActionId[];
}

interface ToolbarGroup {
  readonly id: ToolbarGroupId;
  readonly label: string;
  readonly ariaLabel: string;
  readonly actions: readonly BotLifecycleAction[];
}

const TOOLBAR_GROUPS: readonly ToolbarGroupDefinition[] = [
  {
    id: 'duty',
    label: 'Duty',
    ariaLabel: 'Duty lifecycle controls',
    actionIds: ['confirm_start', 'end_day_now'],
  },
  {
    id: 'roster',
    label: 'Roster',
    ariaLabel: 'Roster controls',
    actionIds: ['add_to_roster', 'take_off_roster'],
  },
  {
    id: 'machinery',
    label: 'Machinery',
    ariaLabel: 'Machinery lifecycle controls',
    actionIds: ['retire_replace'],
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
  readonly actions = input.required<BotLifecycleAction[]>();
  readonly busyAction = input<string | null>(null);
  readonly actionInvoked = output<BotLifecycleActionId>();
  readonly actionTargetHovered = output<string | null>();

  readonly actionsById = computed(() => new Map(this.actions().map((action) => [action.id, action])));
  readonly actionGroups = computed<readonly ToolbarGroup[]>(() => {
    const byId = this.actionsById();
    return TOOLBAR_GROUPS
      .map((group) => ({
        id: group.id,
        label: group.label,
        ariaLabel: group.ariaLabel,
        actions: group.actionIds
          .map((id) => byId.get(id))
          .filter((action): action is BotLifecycleAction => action !== undefined),
      }))
      .filter((group) => group.actions.length > 0);
  });

  trackAction(_: number, action: BotLifecycleAction): BotLifecycleActionId {
    return action.id;
  }

  trackGroup(_: number, group: ToolbarGroup): ToolbarGroupId {
    return group.id;
  }
}
