import type { IbkrConnectionHealth } from '../api/broker-models';

export type LinkState = 'ok' | 'down' | 'warn' | 'unknown';

export type BrokerConnectionState =
  | 'connected'
  | 'soft_lost'
  | 'subscriptions_stale'
  | 'degraded_data_farm'
  | 'reconnecting'
  | 'recovering'
  | 'hard_down'
  | 'disconnected'
  | 'disabled';

export interface BrokerConnectionPresentation {
  state: LinkState;
  headline: string;
  detail: string;
}

const BROKER_CONNECTION_RENDERING: Readonly<Record<
  BrokerConnectionState,
  { state: LinkState; headline: string; baseDetail: string }
>> = {
  connected: { state: 'ok', headline: 'Online', baseDetail: 'Connected' },
  reconnecting: { state: 'warn', headline: 'Reconnecting', baseDetail: 'Reconnecting' },
  recovering: { state: 'warn', headline: 'Recovering', baseDetail: 'Recovering streams' },
  soft_lost: { state: 'warn', headline: 'Degraded', baseDetail: 'Connection degraded — feed lost, recovering' },
  subscriptions_stale: { state: 'warn', headline: 'Degraded', baseDetail: 'Subscriptions stale — resubscribe required' },
  degraded_data_farm: { state: 'warn', headline: 'Degraded', baseDetail: 'IBKR data farm degraded' },
  hard_down: { state: 'down', headline: 'Offline', baseDetail: 'Recovery exhausted' },
  disabled: { state: 'unknown', headline: 'Disabled', baseDetail: 'Disabled' },
  disconnected: { state: 'down', headline: 'Offline', baseDetail: 'Disconnected' },
};

/** Render read-only IBKR connection evidence consistently across broker views. */
export function describeBrokerConnection(
  health: IbkrConnectionHealth | null,
): BrokerConnectionPresentation {
  if (health === null) {
    return { state: 'unknown', headline: 'Checking', detail: 'Waiting for broker health' };
  }

  const state = health.connection_state as BrokerConnectionState;
  const rendering = BROKER_CONNECTION_RENDERING[state];
  const condition = health.condition ?? null;
  if (state === 'reconnecting' && health.reconnect_attempt) {
    return {
      state: rendering.state,
      headline: rendering.headline,
      detail: `${condition?.title ?? rendering.baseDetail} (attempt ${health.reconnect_attempt})`,
    };
  }
  if (state === 'recovering') {
    return {
      state: rendering.state,
      headline: rendering.headline,
      detail: health.recovery_error ? `Recovery failed: ${health.recovery_error}` : rendering.baseDetail,
    };
  }
  if ((state === 'subscriptions_stale' || state === 'degraded_data_farm') && health.last_ibkr_code) {
    return {
      state: rendering.state,
      headline: rendering.headline,
      detail: `${rendering.baseDetail} (${health.last_ibkr_code})`,
    };
  }
  if (state === 'disabled' && health.reason) {
    return { state: rendering.state, headline: rendering.headline, detail: health.reason };
  }
  return {
    state: rendering.state,
    headline: rendering.headline,
    detail: condition?.title ?? rendering.baseDetail,
  };
}
