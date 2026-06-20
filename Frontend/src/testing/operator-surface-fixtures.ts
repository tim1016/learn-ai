// PRD #607 / Slice 1 (#608) — test helpers for the operator-surface
// projection.  Specs that build a ``LiveInstanceStatus`` inline use
// ``DEFAULT_OPERATOR_SURFACE`` to satisfy the now-required
// ``operator_surface`` field without re-specifying every block.

import type { OperatorSurface } from '../app/api/live-instances.types';

/**
 * A benign all-defaults projection useful for fixtures that don't care
 * about cockpit verdicts.  Resume / Pause are enabled as durable-only
 * writes; flatten-and-pause and mark-poisoned are disabled with
 * ``NO_LIVE_BINDING`` (the unbound default).
 */
export const DEFAULT_OPERATOR_SURFACE: OperatorSurface = {
  schema_version: 1,
  host_process: { state: 'UNKNOWN', notice: null, copyable_command: null },
  prior_run: { classification: 'UNKNOWN' },
  broker: { safety_verdict: 'UNKNOWN' },
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
};
