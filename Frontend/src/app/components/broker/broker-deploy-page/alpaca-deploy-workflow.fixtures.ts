/**
 * Shared deployment-readiness fixtures for the Alpaca deploy workflow specs.
 *
 * Lives outside the spec files because two of them need the same baseline
 * view: the workflow spec, and the symbol-scope/staleness spec split out of
 * it once it crossed the 1k-line file rule. Pure data only -- no test-runner
 * imports, because `tsconfig.json` excludes only `*.spec.ts` from the app
 * compilation.
 */

import type { DeployBotView } from '../v2-panel/lib/broker-v2-panel.service';

export const VALIDATION_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'deployment_validation',
  label: 'Deployment Validation',
  explanation: 'Validated canonical decision kernel.',
  validation_case_symbol: 'SPY',
  validation_case_parameters: {},
  golden_validation_scope: false,
  evidence_status: 'accepted',
  paper_access_state: 'enabled',
  selectable: true,
  admissible_modes: ['dry_run', 'paper'],
  override_explanation: null,
  blocked_explanation: null,
};

export const EMA_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'ema_crossover_signal',
  label: 'EMA Crossover Signal',
  explanation: 'Validated EMA(5), EMA(10), and RSI(14) crossover signal.',
  validation_case_symbol: 'SPY',
  validation_case_parameters: {},
  golden_validation_scope: false,
  evidence_status: 'accepted',
  paper_access_state: 'enabled',
  selectable: true,
  admissible_modes: ['dry_run', 'paper'],
  override_explanation: null,
  blocked_explanation: null,
  params_schema: {
    properties: {
      gap: { type: 'number', default: 0.2, title: 'Crossover gap', description: 'Minimum EMA gap for entry.' },
    },
  },
};

export const SMA_OVERRIDE_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'sma_crossover',
  label: 'SMA Crossover',
  explanation: 'Human-validated SMA crossover with evidence-only parity.',
  validation_case_symbol: 'SPY',
  validation_case_parameters: {},
  golden_validation_scope: false,
  evidence_status: 'evidence_only',
  paper_access_state: 'enabled',
  selectable: true,
  admissible_modes: ['dry_run', 'paper'],
  override_explanation: 'Behavioral evidence is evidence-only and not accepted for deployment.',
  blocked_explanation: null,
};

/**
 * Dry Run is the one card every custody world offers, so both views name this
 * const rather than indexing into the other's array — a reorder of the paper
 * fixture must not silently change what the shadow fixture offers.
 */
export const DRY_RUN_EXECUTION_MODE: DeployBotView['execution_modes'][number] = {
  mode: 'dry_run',
  label: 'Dry Run',
  availability: 'available',
  explanation: 'Real market data with simulated decisions and fills only.',
};

export const DEPLOY_VIEW: DeployBotView = {
  broker: 'alpaca',
  account_id: 'PA9',
  account_mode: 'paper',
  account_label: 'Alpaca paper · PA9',
  evaluated_at_ms: 1_700_000_000_000,
  eligibility: {
    eligible: true,
    reason_code: 'ALPACA_PAPER_DEPLOY_READY',
    headline: 'This Alpaca paper account is eligible.',
    explanation: 'Every ENTER and EXIT is executed through the Alpaca Clerk.',
    next_action: 'Review the ticket and deploy the paper bot.',
  },
  dry_run_eligibility: {
    eligible: true,
    reason_code: 'ALPACA_DRY_RUN_READY',
    headline: 'A zero-broker-write Dry Run is available.',
    explanation: 'A registered strategy and a healthy market-data channel are present.',
    next_action: 'Complete the deployment ticket, review the summary, then start the Dry Run.',
  },
  strategies: [VALIDATION_STRATEGY, EMA_STRATEGY, SMA_OVERRIDE_STRATEGY],
  execution_modes: [
    DRY_RUN_EXECUTION_MODE,
    { mode: 'paper', label: 'Paper', availability: 'available', explanation: 'Available through the Alpaca Clerk.' },
    { mode: 'live', label: 'Live', availability: 'planned', explanation: 'Live Alpaca execution is planned.' },
  ],
  readiness_checks: [
    {
      gate_id: 'strategy.validation',
      label: 'Strategy validation',
      ready: true,
      scope: 'strategy',
      authority: 'Strategy validation manifest',
      headline: 'Current accepted evidence is present.',
      explanation: 'The selected strategy has a current human validation receipt.',
      evidence_summary: 'Accepted at the current manifest revision.',
      evidence: { verdict: 'accepted_for_deploy' },
      recovery: null,
    },
    {
      gate_id: 'broker.channel',
      label: 'Broker channel',
      ready: true,
      scope: 'broker',
      authority: 'Alpaca channel health',
      headline: 'Trading and account channels are healthy.',
      explanation: 'The current channel observation admits deployment.',
      evidence_summary: 'Account and trading channels are healthy.',
      evidence: { trading: true, account: true },
      recovery: null,
    },
  ],
  sizing_options: [
    {
      preset: 'safe_canary',
      label: 'Safe canary · 1 share',
      explanation: 'Fixed one-share sizing.',
      min_quantity: 1,
      max_quantity: 1,
      default_quantity: 1,
    },
    {
      preset: 'custom',
      label: 'Bounded custom shares',
      explanation: 'Whole shares from 1 through 100.',
      min_quantity: 1,
      max_quantity: 100,
      default_quantity: 1,
    },
  ],
  action_plan_explanation: 'One long stock ENTER and one matching close-leg EXIT.',
  carryover_available: false,
  carryover_label: 'Allow Clerk-proven exposure carryover on STOP',
  carryover_explanation: 'Account policy currently forbids carried exposure.',
  allowed_actions: ['deploy'],
};

/**
 * The same account seen through the Shadow Account Authority (ADR 0059 D2):
 * a live account whose broker-facing mode is `shadow`, not `paper`. Paper is
 * absent from `execution_modes` entirely — a live account has no paper
 * broker to deploy against — so the form's broker-mode option is Shadow.
 */
export const SHADOW_DEPLOY_VIEW: DeployBotView = {
  ...DEPLOY_VIEW,
  account_id: '9LIVE0001',
  account_mode: 'live',
  account_label: 'Alpaca shadow · 9LIVE0001',
  // Verbatim from the backend's own shadow-world copy table
  // (`paper_deploy_service._deploy_view_copy`). Spreading DEPLOY_VIEW alone
  // left three paper-worded sentences here, which is exactly the leak the
  // shadow specs exist to catch. `reason_code` and `next_action` stay
  // world-neutral in the backend too: they are wire tokens, not prose.
  eligibility: {
    eligible: true,
    reason_code: 'ALPACA_PAPER_DEPLOY_READY',
    headline: 'This Alpaca account is eligible for a Clerk-governed shadow deployment.',
    explanation:
      'The operator may choose Clerk-governed shadow execution — every fill synthesized '
      + "against this live account's real reads, nothing submitted — or a zero-broker-write "
      + 'Dry Run before launch.',
    next_action: 'Complete the deployment ticket, review the summary, then deploy the bot.',
  },
  strategies: DEPLOY_VIEW.strategies.map((strategy) => ({
    ...strategy,
    admissible_modes: strategy.admissible_modes.map((mode) =>
      mode === 'paper' ? 'shadow' : mode,
    ),
  })),
  execution_modes: [
    DRY_RUN_EXECUTION_MODE,
    {
      mode: 'shadow',
      label: 'Shadow',
      availability: 'available',
      explanation: 'Synthesized fills against the live account; nothing is submitted.',
    },
    {
      mode: 'live',
      label: 'Live',
      availability: 'planned',
      // Verbatim from `paper_deploy_service._execution_modes`, the shadow
      // world's planned-live card: shadow is a mode, not a requirement.
      explanation:
        'Real-money submission follows the live cutover and the arming ceremony; a shadow '
        + 'rehearsal is optional (ADR 0059, amended 2026-09-09).',
    },
  ],
};

/** The real-live authority's view: Dry Run and Live, and nothing that lies (ADR 0059 D11, slice 7). */
export const LIVE_DEPLOY_VIEW: DeployBotView = {
  ...SHADOW_DEPLOY_VIEW,
  account_label: 'Alpaca live · 9LIVE0001',
  // Verbatim from the backend's own live-world copy table
  // (`paper_deploy_service._deploy_view_copy`, `custody_world == "real_live"`).
  // SHADOW_DEPLOY_VIEW's eligibility is shadow-worded; spreading it alone
  // would leave "shadow" in a live account's own deploy view.
  eligibility: {
    eligible: true,
    reason_code: 'ALPACA_PAPER_DEPLOY_READY',
    headline: 'This Alpaca live account is eligible for a Clerk-governed live deployment.',
    explanation:
      'The operator may choose Clerk-governed live execution — real-money submission for an '
      + 'armed instance only; every ENTER of an unarmed instance is refused — or a '
      + 'zero-broker-write Dry Run before launch.',
    next_action: 'Complete the deployment ticket, review the summary, then deploy the bot.',
  },
  strategies: SHADOW_DEPLOY_VIEW.strategies.map((strategy) => ({
    ...strategy,
    admissible_modes: strategy.admissible_modes.map((mode) => (mode === 'shadow' ? 'live' : mode)),
  })),
  execution_modes: [
    DRY_RUN_EXECUTION_MODE,
    {
      mode: 'live',
      label: 'Live',
      availability: 'available',
      // Verbatim from `paper_deploy_service._execution_modes`, the live
      // world's broker card.
      explanation:
        'Orders submit real-money trades through the live Clerk for an armed instance only; '
        + 'every ENTER of an unarmed instance is refused until an operator arms it (ADR 0059 D11).',
    },
  ],
};
