import type { OperatorNotice } from '../../../../models/operator-notice';
import { executableOperatorNoticeAction } from '../../../../models/operator-notice-action-contract';
import type { RenderedAction, RendererDispatch } from './suggested-action-renderer';

export interface OperatorNoticeDispatch extends RendererDispatch {
  focusTarget(target: string): void;
  renewControlPlaneLease(): void;
}

export function renderOperatorNoticeAction(
  notice: OperatorNotice,
  dispatch: OperatorNoticeDispatch,
): RenderedAction | null {
  const action = executableOperatorNoticeAction(notice);
  if (action === null) return null;

  switch (action.kind) {
    case 'open_runbook':
      return {
        label: action.label,
        variant: 'link',
        invoke: () => dispatch.openRunbook(action.slug),
      };
    case 'focus_cockpit_action':
      return {
        label: action.label,
        variant: 'link',
        invoke: () => dispatch.focusTarget(action.target),
      };
    case 'redeploy':
      return {
        label: action.label,
        variant: 'link',
        invoke: () => dispatch.redeploy(),
      };
    case 'renew_control_plane_lease':
      return {
        label: action.label,
        variant: 'primary',
        invoke: () => dispatch.renewControlPlaneLease(),
      };
    default: {
      const unreachable: never = action;
      return unreachable;
    }
  }
}
