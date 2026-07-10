import {
  executableOperatorNoticeAction,
  type RenderableNotice,
} from '../../../../models/operator-notice-action-contract';
import type { PresentedAction } from './suggested-action-renderer';

type ExecutableNoticeAction = NonNullable<ReturnType<typeof executableOperatorNoticeAction>>;

export interface PresentedOperatorNoticeAction extends PresentedAction {
  readonly action: ExecutableNoticeAction;
}

/** Pure notice presenter; the consuming container owns typed action dispatch. */
export function presentOperatorNoticeAction(
  notice: RenderableNotice,
): PresentedOperatorNoticeAction | null {
  const action = executableOperatorNoticeAction(notice);
  if (action === null) return null;
  return {
    action,
    label: action.label,
    variant: action.kind === 'renew_control_plane_lease' ? 'primary' : 'link',
  };
}
