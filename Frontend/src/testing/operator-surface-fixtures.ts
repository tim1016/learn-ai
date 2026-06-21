// PRD #607 — test helper for the revised operator_surface contract
// (cockpit revision 2026-06-21).

import type { OperatorSurface } from '../app/api/live-instances.types';

/**
 * A benign all-defaults projection useful for fixtures that don't care
 * about cockpit verdicts.  Resume / Pause enabled as durable-only
 * writes; flatten-and-pause and mark-poisoned disabled with
 * NO_LIVE_BINDING (unbound default).  Trading session is UNKNOWN so
 * tests opt in to a specific phase.
 */
export const DEFAULT_OPERATOR_SURFACE: OperatorSurface = {
  schema_version: 1,
  host_process: { state: 'IDLE', notice: null, copyable_command: null },
  prior_run: { classification: 'UNKNOWN' },
  broker: { safety_verdict: 'UNKNOWN', connection: 'UNKNOWN' },
  configuration: { verdict: 'UNKNOWN', reason_codes: [] },
  current_risk: {
    posture: 'UNKNOWN',
    pending_order_count: null,
    verdict: 'UNKNOWN',
    unrealized_pnl: null,
  },
  daily_order_cap: { used: null, limit: null },
  action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
  actions: {
    resume: { enabled: true, effect: 'DURABLE_ONLY', disabled_reason_code: null },
    pause: { enabled: true, effect: 'DURABLE_ONLY', disabled_reason_code: null },
    flatten_and_pause: {
      enabled: false,
      effect: 'LIVE_ACTUATION',
      disabled_reason_code: 'NO_LIVE_BINDING',
    },
    mark_poisoned: {
      enabled: false,
      effect: 'LIVE_ACTUATION',
      disabled_reason_code: 'NO_LIVE_BINDING',
    },
  },
  trading_session: {
    phase: 'UNKNOWN',
    permits_strategy_activity: null,
    next_transition_ms: null,
    timezone: 'America/New_York',
    as_of_ms: 0,
  },
};
