import type { LifecycleChartActionId } from '../../../api/live-instances.types';

const ACTION_HELP: Record<LifecycleChartActionId, string> = {
  start_process: 'Starts the host-owned bot process when the backend-authored start gate permits it.',
  resume: 'Asks the running bot to resume order-capable behavior.',
  pause: 'Asks the running bot to pause new order placement without changing deploy-time settings.',
  flatten_and_pause: 'Flattens bot-owned exposure, then pauses new order placement.',
  stop: 'Stops the current live process. Stopped runs require redeploy to recover.',
  mark_poisoned: 'Irreversibly marks this run unsafe to resume. Recovery requires a fresh deployment.',
  redeploy: 'Opens deploy with this run prefilled so the next run can use changed settings.',
};

const NODE_HELP: Record<string, string> = {
  deploy: 'Deployment and host-start evidence for this bot.',
  preflight: 'Configuration and readiness checks before live activity.',
  account_safety: 'Broker account ownership, freeze state, and paper-safety evidence.',
  reconcile: 'Proof that broker state and engine state agree before order-capable activity.',
  activate: 'Durable desired-state transition into active behavior.',
  active: 'The running bot loop and current live process state.',
  submit_order: 'The path from strategy signal to order intent and broker submission.',
  broker_writer: 'Broker activity publisher evidence and order/fill observation health.',
  recovery: 'Flatten, halt, poison, and redeploy recovery controls.',
};

const CHIP_HELP: Record<string, string> = {
  broker_proof: 'Broker proof is the backend-authored safety verdict for the broker account evidence.',
  submit: 'Submit is the backend-authored answer to whether the bot may place or manage the next trade.',
  exposure: 'Exposure is the current broker-reported position posture for this bot.',
};

const BUCKET_HELP: Record<string, string> = {
  act_now: 'Live lifecycle controls. Eligibility comes from backend action capabilities.',
  change_for_next_run: 'Deploy-time settings. Changes require a fresh run through redeploy.',
  evidence: 'Read-only proof and provenance. Raw codes may appear here as receipts.',
};

const GENERIC_GATE_HELP = 'Gate evidence explains which backend proof allowed or blocked the lifecycle step.';

export function actionHelp(id: LifecycleChartActionId): string {
  return ACTION_HELP[id];
}

export function nodeHelp(id: string): string {
  return NODE_HELP[id] ?? GENERIC_GATE_HELP;
}

export function chipHelp(id: keyof typeof CHIP_HELP): string {
  return CHIP_HELP[id];
}

export function bucketHelp(id: keyof typeof BUCKET_HELP): string {
  return BUCKET_HELP[id];
}

export function gateHelp(): string {
  return GENERIC_GATE_HELP;
}
