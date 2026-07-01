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

const BUCKET_HELP: Record<string, string> = {
  act_now: 'Live lifecycle controls. Eligibility comes from backend action capabilities.',
  change_for_next_run: 'Deploy-time settings. Changes require a fresh run through redeploy.',
  evidence: 'Read-only proof and provenance. Raw codes stay in hidden diagnostics.',
};

const GENERIC_GATE_HELP = 'Gate evidence explains which backend proof allowed or blocked the lifecycle step.';

export function nodeHelp(id: string): string {
  return NODE_HELP[id] ?? GENERIC_GATE_HELP;
}

export function bucketHelp(id: keyof typeof BUCKET_HELP): string {
  return BUCKET_HELP[id];
}

export function gateHelp(): string {
  return GENERIC_GATE_HELP;
}
