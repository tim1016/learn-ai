import type {
  SqliteExtendedLimitPricing,
  SqliteRecoveryActionCheck,
  SqliteSafeFlattenPlan,
} from '../../../../api/alpaca.types';
import type {
  ActionId,
  BotPanelView,
  PanelAction,
  PanelActionResult,
} from '../lib/broker-v2-panel.types';
import { deriveActionRejection, type ActionRejection } from '../lib/panel-action-outcome';

/** The position the owner confirmed selling: what the plan must match exactly. */
export interface FlattenRequest {
  readonly symbol: string;
  readonly quantity: number;
}

export type FlattenStepId = 'reconcile' | 'plan' | 'sell';
export type FlattenStepState = 'waiting' | 'running' | 'done' | 'failed';

export interface FlattenStepView {
  readonly id: FlattenStepId;
  readonly label: string;
  readonly state: FlattenStepState;
  /** The backend's own words for how the step went, when it said any. */
  readonly message: string | null;
}

const STEP_LABELS: Readonly<Record<FlattenStepId, string>> = {
  reconcile: 'Check the position with Alpaca',
  plan: 'Prepare the sale',
  sell: 'Sell',
};

/** A Dry Run's position lives in its own simulated account, never at Alpaca. */
const DRY_RUN_STEP_LABELS: Readonly<Record<FlattenStepId, string>> = {
  ...STEP_LABELS,
  reconcile: 'Check the position in its simulated account',
};

export function initialFlattenSteps(mode: BotPanelView['mode']): readonly FlattenStepView[] {
  const labels = mode === 'dry_run' ? DRY_RUN_STEP_LABELS : STEP_LABELS;
  return (['reconcile', 'plan', 'sell'] as const).map((id) => ({
    id, label: labels[id], state: 'waiting', message: null,
  }));
}

/**
 * The one way a flatten step changes: the sequence reports through it, and
 * so does the extended-hours ticket that finishes its Sell step.
 */
export function settleFlattenStep(
  steps: readonly FlattenStepView[] | null,
  step: FlattenStepId,
  state: FlattenStepState,
  message: string | null,
): readonly FlattenStepView[] | null {
  return steps?.map((item) => (item.id === step ? { ...item, state, message } : item)) ?? null;
}

/** A step is still running: the owner must not start another flatten over it. */
export function flattenUnderway(steps: readonly FlattenStepView[] | null): boolean {
  return steps?.some((step) => step.state === 'running') ?? false;
}

/** The Sell step's words while the extended-hours ticket waits for the owner's limit. */
const LIMIT_TICKET_OPEN = 'Outside regular hours this sale needs a limit price. Set it below.';

/** What the sequence ended with. */
export type FlattenOutcome =
  | { readonly kind: 'sold'; readonly result: PanelActionResult }
  | {
    readonly kind: 'needs_limit';
    readonly plan: SqliteSafeFlattenPlan;
    readonly pricing: SqliteExtendedLimitPricing;
  }
  | { readonly kind: 'failed'; readonly step: FlattenStepId; readonly rejection: ActionRejection };

/** The reads and commands the sequence runs, bound by the page to the lane,
 * account and bot the owner confirmed on. */
export interface FlattenSequenceDeps {
  readPanel(): Promise<BotPanelView>;
  runAction(action: PanelAction): Promise<PanelActionResult>;
  checkPlan(prepare: PanelAction): Promise<SqliteRecoveryActionCheck>;
  report(step: FlattenStepId, state: FlattenStepState, message: string | null): void;
}

class StepRefused extends Error {
  constructor(readonly rejection: ActionRejection) {
    super(rejection.message);
  }
}

/** The backend's refusal, with its code and — for a closed session — when the next one opens. */
function refusal(
  message: string,
  why: string | null = null,
  reasonCode: string | null = null,
  availableAtMs: number | null = null,
): ActionRejection {
  return { outcome: 'failure', message, why, reasonCode, availableAtMs, nextAction: null };
}

function refused(message: string, why: string | null = null): StepRefused {
  return new StepRefused(refusal(message, why));
}

/** Why a checked sale cannot go out as a prepared plan: no plan at all, in the Clerk's words. */
function unavailablePlan(check: SqliteRecoveryActionCheck): ActionRejection {
  return refusal(
    check.capability.unavailable_reason ?? check.capability.explanation,
    check.capability.next_step,
    check.capability.unavailable_reason_code ?? null,
  );
}

/** Why the Clerk will not price this sale now, or `null` when it will. */
function refusedPricing(check: SqliteRecoveryActionCheck): ActionRejection | null {
  const pricing = check.reduction_pricing ?? null;
  if (pricing === null) return refusal('The prepared sale did not say how it would be sent. Nothing was sent.');
  if (pricing.kind === 'refused') {
    return refusal(pricing.explanation, pricing.next_step, pricing.reason_code, pricing.available_at_ms ?? null);
  }
  return null;
}

/** The named command as a fresh panel presents it, enabled, or the backend's reason it is not. */
function presentedAction(panel: BotPanelView, actionId: ActionId, name: string): PanelAction {
  const action = panel.actions.find((candidate) => candidate.action_id === actionId);
  if (action === undefined) throw refused(`This bot does not offer ${name} right now.`);
  if (!action.enabled) {
    const blocker = action.blockers[0];
    throw refused(blocker?.headline ?? action.explanation, blocker?.detail ?? null);
  }
  return action;
}

function describePlan(plan: SqliteSafeFlattenPlan): string {
  return plan.legs.map((leg) => `${leg.side} ${leg.quantity} ${leg.symbol}`).join(', ');
}

/** The plan sells exactly the one position the owner confirmed, or nothing is sent. */
function verifiedPlan(check: SqliteRecoveryActionCheck, request: FlattenRequest): SqliteSafeFlattenPlan {
  const plan = check.capability.reduction_plan;
  if (plan === null) throw new StepRefused(unavailablePlan(check));
  const [leg] = plan.legs;
  const matches = plan.legs.length === 1
    && leg.symbol === request.symbol
    && leg.quantity === request.quantity;
  if (!matches) {
    throw refused(
      `The prepared sale (${describePlan(plan)}) does not match the ${request.quantity} `
        + `${request.symbol} you confirmed. Nothing was sent.`,
      'Review the position, then flatten again.',
    );
  }
  return plan;
}

/**
 * Flatten a stopped bot's position in one confirmed sequence (hurdle H30).
 *
 * The Clerk accepts a flatten only on fresh recovery evidence (its window is
 * `FRESH_EVIDENCE_MAX_AGE_MS`, 30 s), so the steps run back to back rather
 * than across three separate clicks: reconcile the bot, re-read the panel for
 * the tokens that reconciliation minted, prepare the plan and check it sells
 * exactly what the owner confirmed, then send it. Every step's refusal is the
 * backend's own reason, and the sequence stops at the first one.
 *
 * During regular hours the prepared sale is sent at market. Outside them the
 * sale needs a limit price, so the sequence stops at `needs_limit` and the
 * page opens the extended-hours ticket on the verified plan. A refused
 * pricing (a closed session) fails the sell step with the Clerk's words.
 */
export async function runFlattenSequence(
  deps: FlattenSequenceDeps,
  request: FlattenRequest,
): Promise<FlattenOutcome> {
  let step: FlattenStepId = 'reconcile';
  try {
    deps.report(step, 'running', null);
    const reconcile = presentedAction(await deps.readPanel(), 'reconcile_now', 'Reconcile now');
    deps.report(step, 'done', (await deps.runAction(reconcile)).message);

    step = 'plan';
    deps.report(step, 'running', null);
    const prepare = presentedAction(await deps.readPanel(), 'prepare_safe_flatten', 'a prepared sale');
    const check = await deps.checkPlan(prepare);
    const plan = verifiedPlan(check, request);
    deps.report(step, 'done', check.capability.next_step);

    step = 'sell';
    deps.report(step, 'running', null);
    const pricing = check.reduction_pricing ?? null;
    if (pricing?.kind === 'extended_limit') {
      deps.report(step, 'running', LIMIT_TICKET_OPEN);
      return { kind: 'needs_limit', plan, pricing };
    }
    const pricingRefusal = refusedPricing(check);
    if (pricingRefusal !== null) throw new StepRefused(pricingRefusal);
    const execute = presentedAction(await deps.readPanel(), 'execute_safe_flatten', 'the prepared sale');
    const result = await deps.runAction(execute);
    deps.report(step, 'done', result.message);
    return { kind: 'sold', result };
  } catch (error) {
    const rejection = error instanceof StepRefused
      ? error.rejection
      : deriveActionRejection(error, `${STEP_LABELS[step]} failed.`);
    deps.report(step, 'failed', rejection.message);
    return { kind: 'failed', step, rejection };
  }
}

/** What a quote refresh leaves of an open extended-hours ticket. */
export type RefreshedLimitTicket =
  | {
    readonly kind: 'ticket';
    readonly plan: SqliteSafeFlattenPlan;
    readonly pricing: SqliteExtendedLimitPricing;
  }
  | { readonly kind: 'ended'; readonly rejection: ActionRejection };

/**
 * Read a quote refresh of the extended-hours ticket (#2007): the plan still
 * goes out as a limit order, or the ticket ends with the Clerk's reason —
 * the plan is gone, the session closed, or the regular session opened and
 * the sale no longer takes a limit. An ended ticket finishes the Sell step.
 */
export function refreshedLimitTicket(check: SqliteRecoveryActionCheck): RefreshedLimitTicket {
  const plan = check.capability.reduction_plan;
  if (plan === null) return { kind: 'ended', rejection: unavailablePlan(check) };
  const pricing = check.reduction_pricing ?? null;
  if (pricing?.kind === 'extended_limit' && plan.legs.length === 1) return { kind: 'ticket', plan, pricing };
  return {
    kind: 'ended',
    rejection: refusedPricing(check) ?? refusal(
      'The regular session has opened, so this sale no longer takes a limit price. Nothing was sent.',
      'Flatten again to sell at market.',
    ),
  };
}
