import type { OperatorNotice } from './operator-notice';

/** Structural subset of OperatorNotice that the shared notice renderer and
 *  action contract consume. MutationRungReceipt satisfies it too, so
 *  notice-shaped receipts render through the same component without a cast. */
export type RenderableNotice = Pick<
  OperatorNotice,
  'tier' | 'actionability' | 'title' | 'message' | 'resolution' | 'action' | 'forensic_facts'
> & { runbook_slug?: string | null };

export type ExecutableOperatorNoticeAction =
  | {
      kind: 'open_runbook';
      label: string;
      slug: string;
    }
  | {
      kind: 'focus_cockpit_action';
      label: string;
      target: string;
    }
  | {
      kind: 'renew_control_plane_lease';
      label: string;
    }
  | {
      kind: 'redeploy';
      label: string;
    };

export function executableOperatorNoticeAction(
  notice: RenderableNotice,
): ExecutableOperatorNoticeAction | null {
  const { action } = notice;
  if (!action.label) return null;

  switch (action.kind) {
    case 'open_runbook': {
      const slug = action.target ?? notice.runbook_slug;
      return slug ? { kind: action.kind, label: action.label, slug } : null;
    }
    case 'focus_cockpit_action':
      return action.target
        ? { kind: action.kind, label: action.label, target: action.target }
        : null;
    case 'renew_control_plane_lease':
    case 'redeploy':
      return { kind: action.kind, label: action.label };
    case 'external_manual_check':
    case 'none':
      return null;
    default: {
      const unreachable: never = action.kind;
      return unreachable;
    }
  }
}
