import type {
  ActionId,
  BotPanelView,
  PanelAction,
} from '../lib/broker-v2-panel.types';
import type { PanelActionTone } from '../panel-action-button/panel-action-button.component';

/**
 * Canonical action-id -> visual tone, shared by both banners and the
 * Operator readiness accordion. WHICH action is primary is backend-owned
 * (`BotPanelView.primary_action_by_lens`, issue #1665); this map only
 * controls presentation tone, which stays frontend-owned per the issue's
 * scope ("Keep visual tone and component layout frontend-owned after the
 * backend has selected the action").
 */
export const ACTION_TONES: Partial<Record<ActionId, PanelActionTone>> = {
  resume: 'primary',
  pause: 'warning',
  continue: 'primary',
  stop: 'danger',
  stop_bot_decisions: 'danger',
  flatten_stop: 'danger',
  reconcile_now: 'neutral',
  recover_exact_execution_evidence: 'warning',
  resolve_execution_coverage: 'warning',
  cancel_verified_working_orders: 'danger',
  prepare_safe_flatten: 'neutral',
  execute_safe_flatten: 'danger',
  open_custody_timeline: 'neutral',
  rebuild_from_mirror: 'warning',
  reset_authority: 'danger',
};

/** The tone for one action, or the neutral-safe 'primary' default when unmapped. */
export function actionTone(action: PanelAction | null): PanelActionTone {
  if (action === null) return 'primary';
  return ACTION_TONES[action.action_id] ?? 'primary';
}

type LensPanel = Pick<BotPanelView, 'actions' | 'primary_action_by_lens'>;

/**
 * Resolve one lens's backend-selected banner action (issue #1665).
 *
 * The backend authors `primary_action_by_lens`; this only looks up the
 * `PanelAction` object the id names. A missing or dangling reference (an id
 * that does not match any presented action) fails closed to `null` — the
 * banner renders no primary action rather than falling back to a
 * health-derived guess.
 */
export function primaryActionForLens(
  panel: LensPanel,
  lens: keyof BotPanelView['primary_action_by_lens'],
): PanelAction | null {
  const actionId = panel.primary_action_by_lens[lens];
  if (actionId === null) return null;
  return panel.actions.find((action) => action.action_id === actionId) ?? null;
}
