import type { ResourceTarget } from '../../../fleet/resource-target';
import type {
  BotClearRequest,
  CohortActionResult,
  CohortLegResult,
} from '../../broker/v2-panel/lib/broker-v2-panel.types';

/**
 * Clearing finished bots from Home's Finished fold (owner decision
 * 2026-09-28): a cleared bot leaves Home and the catalog, and its history
 * stays in Activity. Each leg is that bot's own archive, re-proven by the
 * backend under its lock, so a bot that is still holding is refused in the
 * backend's own words rather than filtered here.
 */

/** The most bots one clear may carry: `BotClearRequest.strategy_instance_ids`
 * is `max_length=256` in `app/schemas/broker_v2_panel.py`, and the generated
 * TS types do not carry array bounds. */
export const MAX_CLEAR_BOTS = 256;

/** One confirmed clear: the exact request and the lane it was sent to. A
 * retry re-sends this object unchanged — same key, same bots — so a bot that
 * was already cleared replays rather than clearing twice. */
export interface ClearBatch {
  readonly target: ResourceTarget;
  readonly request: BotClearRequest;
}

/** What the last clear did: a typed per-bot result, or why the request as a
 * whole returned none. `unknown` is a transport failure — the batch may or may
 * not have run, so re-sending the same batch is the cure. */
export type ClearOutcome =
  | { readonly kind: 'result'; readonly result: CohortActionResult; readonly requested: readonly string[] }
  | { readonly kind: 'unknown' }
  | {
      readonly kind: 'refused';
      readonly message: string;
      readonly why: string | null;
      readonly reasonCode: string | null;
    };

/** A leg the backend cleared: this request applied it, or an earlier send
 * under the same key already had. */
export function isCleared(leg: CohortLegResult): boolean {
  return leg.outcome === 'applied' || leg.outcome === 'replayed';
}

/**
 * Whether re-sending the same batch could change anything: a bot the batch
 * never reached (it ends early on an account-scoped refusal), or one whose
 * outcome failed or is unknown. A refusal is the backend's answer about that
 * bot — a holding bot must be flattened first, then cleared afresh — so it
 * does not earn a retry of the same batch.
 */
export function clearRetryCanChange(outcome: ClearOutcome): boolean {
  if (outcome.kind === 'unknown') return true;
  if (outcome.kind === 'refused') return false;
  const { result, requested } = outcome;
  return (
    result.legs.length < requested.length ||
    result.legs.some((leg) => leg.outcome === 'failed' || leg.outcome === 'unknown')
  );
}

function bots(count: number): string {
  return count === 1 ? 'bot' : 'bots';
}

/** The closed operator-copy map for clearing finished bots. Backend refusal
 * prose (`message` / `why`) is rendered as written and never passes through
 * here; the functions interpolate counts only. */
export const HOME_CLEAR_COPY = {
  selectAll: 'Select all finished bots',
  select: (sid: string) => `Select ${sid}`,
  selected: (count: number) => (count === 0 ? 'No finished bots selected.' : `${count} selected.`),
  clearSelected: (count: number) => `Clear selected (${count})`,
  clearAll: 'Clear all finished',
  overCap: `At most ${MAX_CLEAR_BOTS} bots can be cleared at once.`,
  confirmHeading: (count: number) => `Clear ${count} finished ${bots(count)} from Home?`,
  confirmMessage: (count: number) =>
    `${count === 1 ? 'Its' : 'Their'} history stays in Activity: runs, fills, fees and results.`,
  confirmConsequence: 'This can’t be undone.',
  confirmLabel: (count: number) => `Clear ${count}`,
  clearing: (count: number) => `Clearing ${count} ${bots(count)}…`,
  summary: (cleared: number, sent: number) => {
    const rest = sent - cleared;
    return `Cleared ${cleared} of ${sent} ${bots(sent)}.` + (rest === 0 ? '' : ` ${rest} not cleared.`);
  },
  legLabel: {
    applied: 'Cleared',
    replayed: 'Cleared',
    refused: 'Not cleared',
    failed: 'Failed',
    unknown: 'Outcome unknown',
  } satisfies Readonly<Record<CohortLegResult['outcome'], string>>,
  notAttempted: 'Not reached, safe to try again:',
  requestUnknown:
    'The clear did not reach a result. Nothing is confirmed either way; trying again re-sends ' +
    'this same request, so a bot already cleared is not cleared twice.',
  retry: 'Try again',
  dismiss: 'Dismiss',
  requestFallback: 'The clear was refused.',
} as const;
