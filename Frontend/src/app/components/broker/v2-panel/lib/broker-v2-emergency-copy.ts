/**
 * Emergency-fallback copy for the broker-v2 panel closed vocabulary.
 *
 * AUTHORITY: Backend-authored prose is the sole semantic-copy authority.
 * This map is an explicit emergency fallback used ONLY when the server
 * omits required copy (label/explanation) for an emitted vocabulary code.
 *
 * DO NOT use this map as the primary copy source. A contract test in
 * broker-v2-copy-contract.spec.ts fails visibly when server copy is
 * missing, surfacing the gap immediately.
 *
 * This map is locked against the vocabulary snapshot at
 * lib/broker-v2-vocabulary.snapshot.json. Adding a vocabulary code
 * requires updating this map AND the snapshot AND vocabulary.py.
 */

export interface VocabularyCopy {
  readonly label: string;
  readonly explanation: string;
}

export const BROKER_V2_EMERGENCY_COPY: Readonly<Record<string, VocabularyCopy>> = {
  BROKER_ACK: {
    label: 'Broker ack',
    explanation: 'The broker acknowledged (or rejected) the submitted order.',
  },
  CRASHED: {
    label: 'Crashed',
    explanation: 'The bot exited on an unhandled error. Check the duty reason.',
  },
  EXITED_UNVERIFIED: {
    label: 'Exited unverified',
    explanation: "The bot's task ended without a clean stop. Its final state is not confirmed.",
  },
  FILL: {
    label: 'Fill',
    explanation: 'The order executed, in full or in part, at the broker.',
  },
  INTENT: {
    label: 'Intent',
    explanation: 'The bot recorded an order intent before touching the broker.',
  },
  NO_HOLD: {
    label: 'No hold',
    explanation: 'No exposure hold is active. Order submission is allowed.',
  },
  OFF_DUTY: {
    label: 'Off duty',
    explanation: 'The bot is not running. It evaluates no bars and places no orders.',
  },
  PAUSED: {
    label: 'Paused',
    explanation:
      'The current run remains alive but bar evaluation is held until Continue.',
  },
  ON_DUTY: {
    label: 'On duty',
    explanation: 'The bot is running and evaluating bars as they close.',
  },
  RECONCILED: {
    label: 'Reconciled',
    explanation: 'A sweep confirmed the journal and the broker agree on this order.',
  },
  RETIRED: {
    label: 'Retired',
    explanation: 'The bot is permanently decommissioned. Its id is never reused.',
  },
  RUNNING: {
    label: 'Running',
    explanation: 'The operator wants this bot evaluating bars.',
  },
  SIGNAL: {
    label: 'Signal',
    explanation: 'The bot evaluated a bar and produced (or withheld) a decision.',
  },
  STOPPED: {
    label: 'Stopped',
    explanation: 'The operator wants this bot idle. Exposure is left untouched.',
  },
  STOPPED_OUTCOME: {
    label: 'Stopped cleanly',
    explanation: 'The bot exited on an operator stop or a service shutdown.',
  },
  STREAM_HEALTH_HOLD: {
    label: 'Stream-health hold',
    explanation:
      'A market-data or execution channel is unhealthy. New submits are paused account-wide.',
  },
  SUBMIT_GATE: {
    label: 'Submit gate',
    explanation: 'Holds and channel health were checked before submission.',
  },
  UNEXPLAINED_ORDER_HOLD: {
    label: 'Unexplained-order hold',
    explanation:
      'An order this account did not submit was seen in the journal. New submits are paused account-wide.',
  },
  blocked: {
    label: 'Blocked',
    explanation: 'An identified condition is preventing this station from progressing.',
  },
  cancel_order: {
    label: 'Cancel order',
    explanation: 'Cancel one working order at the broker.',
  },
  cancel_verified_working_orders: {
    label: 'Cancel verified working orders',
    explanation:
      'Cancel only working orders whose exact Clerk and broker identities are proven.',
  },
  clean: {
    label: 'Clean',
    explanation: 'The last sweep found the journal and the broker in agreement.',
  },
  deploy: {
    label: 'Deploy',
    explanation: 'Create and start a new bot bound to this account.',
  },
  flatten_stop: {
    label: 'Flatten & stop',
    explanation: 'Cancel working orders, submit closing orders to flatten exposure, then stop.',
  },
  healthy: {
    label: 'Healthy',
    explanation: 'The channel is connected and current.',
  },
  missing_intent: {
    label: 'Missing intent',
    explanation:
      'The last sweep found broker inventory or an owned order that does not match the durable journal exposure.',
  },
  open_custody_timeline: {
    label: 'Open custody timeline',
    explanation: 'Inspect the immutable operation-first evidence timeline.',
  },
  not_applicable: {
    label: 'Not applicable',
    explanation: 'This broker or mode has no such station.',
  },
  reconcile_now: {
    label: 'Reconcile now',
    explanation: 'Run a reconciliation sweep against the broker immediately.',
  },
  recover_exact_execution_evidence: {
    label: 'Recover exact execution evidence',
    explanation:
      'Read one retained Alpaca paper execution and prepare the Clerk\'s no-delta coverage proof.',
  },
  resolve_execution_coverage: {
    label: 'Resolve execution coverage',
    explanation:
      'Replace one matching cumulative recovery record with verified exact execution evidence.',
  },
  prepare_safe_flatten: {
    label: 'Prepare safe flatten',
    explanation: 'Prepare a fresh reduction plan without submitting an order.',
  },
  rebuild_from_mirror: {
    label: 'Rebuild from mirror',
    explanation: 'Rebuild a failed authority only from a contiguous verified mirror.',
  },
  retire: {
    label: 'Retire',
    explanation: 'Permanently decommission this bot. Its id is never reused.',
  },
  satisfied: {
    label: 'Satisfied',
    explanation: 'This station completed with recorded evidence.',
  },
  stale: {
    label: 'Stale',
    explanation: 'The last sweep could not reach the broker; the verdict is out of date.',
  },
  resume: {
    label: 'Resume',
    explanation:
      'Create a new run of this unchanged strategy instance after backend admission.',
  },
  reset_authority: {
    label: 'Reset authority',
    explanation:
      'Create a new authority generation only after fresh flat and order-free proof.',
  },
  pause: {
    label: 'Pause',
    explanation:
      'Hold bar evaluation while keeping the current process and run identity alive.',
  },
  continue: {
    label: 'Continue',
    explanation:
      'Let this paused live run evaluate bars again without changing its run ID.',
  },
  stop: {
    label: 'Stop',
    explanation:
      "Stop evaluating bars and cancel this bot's working entry orders. Exposure is left untouched.",
  },
  stop_bot_decisions: {
    label: 'Stop bot decisions',
    explanation:
      'Stop new decisions while existing exposure remains under Clerk custody.',
  },
  unexplained_order: {
    label: 'Unexplained order',
    explanation: 'The last sweep found a broker order the journal cannot explain.',
  },
  unhealthy: {
    label: 'Unhealthy',
    explanation: 'The channel is down or lagging. Trading is gated until it recovers.',
  },
  unknown: {
    label: 'Unknown',
    explanation: "The channel's health has not been observed yet.",
  },
  unknown_stale: {
    label: 'Unknown (stale)',
    explanation: 'Evidence for this station exists but is too old to trust.',
  },
  waiting: {
    label: 'Waiting',
    explanation: 'This station is expected to progress. Nothing is wrong.',
  },
};
