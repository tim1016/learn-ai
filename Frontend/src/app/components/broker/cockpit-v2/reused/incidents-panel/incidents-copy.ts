import type { IncidentCategory } from './incidents.types';

/**
 * Severity tone for an incident row. Drives the row tint and the
 * "this is urgent" reading the operator gets at a glance.
 *
 * - `warning` — degraded but the bot can continue (e.g., transient
 *   broker disconnect being auto-recovered).
 * - `critical` — the bot has stopped acting, action recommended
 *   (engine fatal, broker reconnect failed).
 * - `blocking` — irreversible-without-redeploy state (operator halt,
 *   poisoned). The same run cannot resume.
 * - `unknown` — fresh failure mode the catalog does not know about
 *   yet; the panel surfaces the raw traceback for engineering.
 */
export type IncidentSeverity = 'warning' | 'critical' | 'blocking' | 'unknown';

export interface IncidentCopy {
  /** Card-style short title shown on the row. */
  title: string;
  /** One-sentence operator-language explanation of what happened. */
  message: string;
  /** Severity tone — drives row tint + sort-by-attention. */
  severity: IncidentSeverity;
  /** Plain-English next step the operator should take. */
  recommendedAction: string;
}

/**
 * Trader-language copy for every backend-defined `IncidentCategory`.
 *
 * The map keys on the backend enum so the frontend never re-derives
 * meaning from raw log text. Copy iteration ships in a frontend release
 * without a Python rebuild. The `UNKNOWN` entry is the rollout-safety
 * fallback when the backend emits a category the frontend hasn't seen
 * yet (or omits it).
 */
export const INCIDENT_COPY: Record<IncidentCategory, IncidentCopy> = {
  broker_disconnect: {
    title: 'Broker connection lost',
    message: 'The IBKR session dropped. The engine is waiting for the broker to come back.',
    severity: 'warning',
    recommendedAction:
      'No action needed if the connection recovers within a few minutes. Check IBKR Gateway / TWS if it persists.',
  },
  broker_reconnect_failed: {
    title: 'Broker reconnect failed',
    message:
      "The engine tried to reconnect to IBKR and couldn't confirm the session is healthy.",
    severity: 'critical',
    recommendedAction:
      'Open IBKR Gateway / TWS and confirm it is logged in. Restart the bot once the session is verified.',
  },
  engine_fatal: {
    title: 'Engine stopped unexpectedly',
    message: 'An unhandled exception killed the engine. No new orders are being placed.',
    severity: 'critical',
    recommendedAction:
      'Open the raw log to see the traceback, then stop the bot and verify positions before restarting.',
  },
  portfolio_init_fail: {
    title: 'Portfolio could not start',
    message:
      'The engine refused to initialize the live portfolio (start-time invariants failed).',
    severity: 'critical',
    recommendedAction:
      'Open the raw log for the rejection reason, then re-deploy with the corrected configuration.',
  },
  reconcile_missing: {
    title: 'No reconciliation receipt',
    message:
      'The engine could not find the reconciliation receipt it expected at startup.',
    severity: 'critical',
    recommendedAction:
      'Verify the broker account state matches the cockpit before restarting the bot.',
  },
  lost_fill: {
    title: 'Lost fill — bot halted',
    message:
      'An order the bot placed never confirmed a fill within its window. The bot halted to protect the account.',
    severity: 'blocking',
    recommendedAction:
      'Reconcile the broker account and re-deploy a fresh run_id. The same run cannot resume.',
  },
  outside_mutation: {
    title: 'Outside mutation — bot halted',
    message:
      'A trade the bot did not place was seen on the account. The bot halted rather than trade against an unknown position.',
    severity: 'blocking',
    recommendedAction:
      'Reconcile the broker account and re-deploy a fresh run_id. The same run cannot resume.',
  },
  cold_start_divergence: {
    title: 'Cold-start divergence — bot halted',
    message:
      "On startup the bot couldn't reconcile its own records against the broker. It refused to resume on stale state.",
    severity: 'blocking',
    recommendedAction:
      'Reconcile the broker account and re-deploy a fresh run_id. The same run cannot resume.',
  },
  operator_halt: {
    title: 'Operator-declared halt',
    message: 'An operator manually flagged this run unsafe.',
    severity: 'blocking',
    recommendedAction:
      'Reconcile the broker account and re-deploy a fresh run_id when ready to resume trading.',
  },
  subscription_stale: {
    title: 'Bar subscription stalled',
    message:
      'The live bar subscription absorbed too many redelivered events. The feed may be stale.',
    severity: 'warning',
    recommendedAction:
      'No action needed if bars resume in the next minute. Restart the bot if the stall persists.',
  },
  unknown: {
    title: 'Unknown error — see raw traceback',
    message:
      'A failure mode the catalog does not recognize yet. The raw traceback is preserved for engineering.',
    severity: 'unknown',
    recommendedAction:
      'Open the raw log to see the original message and share it with engineering if it recurs.',
  },
};

/** Look up the copy for an incident category, falling back to UNKNOWN
 * when the backend emits a category the frontend has not seen yet
 * (rollout safety) or no category at all. */
export function getIncidentCopy(category: IncidentCategory | null | undefined): IncidentCopy {
  if (category === null || category === undefined) return INCIDENT_COPY.unknown;
  return INCIDENT_COPY[category] ?? INCIDENT_COPY.unknown;
}
