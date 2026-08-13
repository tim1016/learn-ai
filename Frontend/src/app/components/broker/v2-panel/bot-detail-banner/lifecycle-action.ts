import type {
  ActionId,
  BotPanelView,
  PanelAction,
} from '../lib/broker-v2-panel.types';

const STOP_ACTION_IDS: readonly ActionId[] = ['stop', 'stop_bot_decisions'];

type LifecycleActionPanel = Pick<BotPanelView, 'actions' | 'health'>;

/** Returns the one lifecycle action that belongs in a bot-detail banner. */
export function primaryLifecycleAction(panel: LifecycleActionPanel): PanelAction | null {
  const { health } = panel;
  const actionIds: readonly ActionId[] = !health.running
    ? ['resume']
    : health.desired_state === 'PAUSED'
      ? ['continue']
      : STOP_ACTION_IDS;
  return panel.actions.find((action) => actionIds.includes(action.action_id)) ?? null;
}

export function lifecycleActionTone(action: PanelAction): 'primary' | 'danger' {
  return STOP_ACTION_IDS.includes(action.action_id) ? 'danger' : 'primary';
}
