import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  HostProcessStartDisabledReasonCode,
  LifecycleChartAction,
  LifecycleChartActionId,
  LiveInstanceStatus,
} from '../../../../api/live-instances.types';
import { actionHelp } from '../concept-help.registry';
import { disabledReasonCopy } from '../../cockpit-v2/lib/disabled-reason-copy';

type EmergencyActionId = Extract<
  LifecycleChartActionId,
  'start_process' | 'resume' | 'pause' | 'flatten_and_pause' | 'stop'
>;

interface RenderedLifecycleAction {
  readonly action: LifecycleChartAction;
  readonly reasonCode: string | null;
  readonly traderCopy: string | null;
}

const EMERGENCY_ACTION_ORDER: readonly EmergencyActionId[] = [
  'start_process',
  'resume',
  'pause',
  'flatten_and_pause',
  'stop',
];

const START_REASON_COPY: Record<HostProcessStartDisabledReasonCode, string> = {
  ALREADY_RUNNING: 'The host runner is already running for this bot.',
  STOPPING: 'The host runner is stopping. Wait for it to finish before starting again.',
  HOST_SERVICE_OFFLINE: 'The host service is offline. Start or reconnect the host runner first.',
  STOPPED_REQUIRES_REDEPLOY: 'This run is stopped. Start requires a fresh redeploy.',
  START_SETTINGS_INCOMPLETE: 'Start settings are incomplete. Redeploy with the missing settings.',
  ACCOUNT_FROZEN: 'The account is frozen. Resolve the account freeze before starting.',
};

@Component({
  selector: 'app-overview-actions',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './overview-actions.component.html',
  styleUrl: './overview-actions.component.scss',
})
export class OverviewActionsComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly actions = input.required<LifecycleChartAction[]>();
  readonly busyAction = input<string | null>(null);
  readonly actionInvoked = output<LifecycleChartActionId>();
  readonly disabledActionSelected = output<string>();
  readonly actionTargetHovered = output<string | null>();

  readonly emergencyActions = computed(() => {
    const byId = this.actionsById();
    return EMERGENCY_ACTION_ORDER
      .map((id) => byId.get(id))
      .filter((action): action is LifecycleChartAction => action !== undefined)
      .map((action) => this.renderAction(action));
  });
  readonly redeployAction = computed(() => {
    const action = this.actionsById().get('redeploy');
    return action ? this.renderAction(action) : null;
  });
  readonly overflowActions = computed(() => {
    const action = this.actionsById().get('mark_poisoned');
    return action ? [this.renderAction(action)] : [];
  });

  actionHelp(action: LifecycleChartAction): string {
    return actionHelp(action.id);
  }

  activateAction(action: LifecycleChartAction): void {
    if (!action.enabled || this.busyAction() !== null) {
      if (action.target_node_id) {
        this.disabledActionSelected.emit(action.target_node_id);
      }
      return;
    }
    this.actionInvoked.emit(action.id);
  }

  hoverAction(action: LifecycleChartAction | null): void {
    this.actionTargetHovered.emit(action?.target_node_id ?? null);
  }

  trackAction(_: number, rendered: RenderedLifecycleAction): LifecycleChartActionId {
    return rendered.action.id;
  }

  private actionsById(): Map<LifecycleChartActionId, LifecycleChartAction> {
    return new Map(this.actions().map((action) => [action.id, action]));
  }

  private renderAction(action: LifecycleChartAction): RenderedLifecycleAction {
    const reasonCode = this.disabledReasonCode(action);
    return {
      action,
      reasonCode,
      traderCopy: action.enabled ? null : this.disabledTraderCopy(action, reasonCode),
    };
  }

  private disabledReasonCode(action: LifecycleChartAction): string | null {
    if (action.enabled) return null;
    const surface = this.status().operator_surface;
    switch (action.id) {
      case 'start_process':
        return surface.host_process.start_capability.disabled_reason_code;
      case 'resume':
      case 'pause':
      case 'flatten_and_pause':
      case 'stop':
      case 'mark_poisoned':
        return surface.actions[action.id].disabled_reason_code;
      case 'redeploy':
        return null;
    }
  }

  private disabledTraderCopy(action: LifecycleChartAction, reasonCode: string | null): string {
    if (action.id === 'start_process') {
      return this.startReasonCopy(reasonCode) ?? 'The host runner cannot be started yet.';
    }
    if (action.id === 'redeploy') {
      return action.reason ?? 'Redeploy creates a new run with updated settings.';
    }
    const copy = disabledReasonCopy(reasonCode);
    if (copy?.startsWith('Unrecognized reason code:')) {
      return 'This action is unavailable until the backend reason is recognized.';
    }
    return copy ?? 'This action is unavailable right now.';
  }

  private startReasonCopy(reasonCode: string | null): string | null {
    if (!reasonCode) return null;
    if (Object.prototype.hasOwnProperty.call(START_REASON_COPY, reasonCode)) {
      return START_REASON_COPY[reasonCode as HostProcessStartDisabledReasonCode];
    }
    return 'The host runner cannot be started until the start gate clears.';
  }
}
