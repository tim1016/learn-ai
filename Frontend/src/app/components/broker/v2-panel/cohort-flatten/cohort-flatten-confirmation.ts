import type { CohortFlattenLeg } from '../lib/broker-v2-panel.types';

/**
 * The token an operator types to confirm a cohort flatten.
 *
 * The `required_token` the backend puts on the single-bot `flatten_stop`
 * confirmation, so one word means one thing on both surfaces. Flattening N
 * bots is strictly more consequential than flattening one, so the batch asks
 * for the same deliberate act once rather than waiving it because it is bulk.
 */
export const FLATTEN_CONFIRM_TOKEN = 'FLATTEN';

export interface CohortFlattenConfirmation {
  readonly heading: string;
  readonly message: string;
  readonly consequence: string;
  readonly confirmLabel: string;
}

/** `SYM qty` pairs, in the per-bot `flatten_stop` body's own format. */
export function formatExposure(exposure: Readonly<Record<string, number>>): string {
  return (
    Object.entries(exposure)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([symbol, quantity]) => `${symbol} ${quantity}`)
      .join(', ') || 'none'
  );
}

/**
 * The blast-radius copy for one wave (ADR 0051 Decision 6).
 *
 * Modelled on the per-bot `flatten_stop` confirmation: which account, how many
 * bots, and each bot's attributed exposure — the quantities the confirmed
 * tokens will reduce, from the same panel cut that armed them.
 */
export function cohortFlattenConfirmation(
  accountId: string,
  strategyLabel: string,
  legs: readonly CohortFlattenLeg[],
): CohortFlattenConfirmation {
  const count = legs.length;
  const bots = count === 1 ? 'bot' : 'bots';
  const exposure = legs
    .map((leg) => `${leg.strategy_instance_id} ${formatExposure(leg.exposure)}`)
    .join('; ');
  return {
    heading: `Flatten ${count} ${bots} in this cohort?`,
    message:
      `This command targets ${count} ${bots} of ${strategyLabel} on account ${accountId}. ` +
      `Attributed exposure: ${exposure}.`,
    consequence:
      'Each bot runs its own flatten in turn and reduces only its Clerk-attributed ' +
      'exposure; no other bot’s or manual position on this account is touched. A ' +
      'refused bot does not stop the others, and fills may complete later.',
    confirmLabel: `Flatten ${count}`,
  };
}
