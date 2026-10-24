/**
 * What the Cross-sectional page says, before Run, about the stock bars the lake does not hold for the universe (#3524).
 *
 * Each member is counted on its own by the shared lake-holding control (`createLakeHolding`): its sessions come from
 * the server, and so does the span a capture asks for. This adds the one thing a universe has that a single
 * instrument does not: the total. A cold member costs about one vendor call per session, so the total is what tells a
 * reader how slow the first run will be.
 */
import type { WorkspaceStateContent } from '../../../../shared/workspace/workspace-state';

/** The members' counts added up. A member whose count could not be read adds nothing and says so itself. */
export interface UniverseHoldingTally {
  /** Members counted: the universe as entered. */
  readonly members: number;
  /** Members with at least one session a capture can fetch. */
  readonly lacking: number;
  /** Sessions a capture can fetch, summed over the members: one vendor call each. */
  readonly memberSessions: number;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** The summary above the per-member notices; `null` when the lake holds every session of every counted member. */
export function describeUniverseHolding(tally: UniverseHoldingTally): WorkspaceStateContent | null {
  if (tally.memberSessions === 0) return null;
  return {
    kind: 'partial',
    happened: `${plural(tally.memberSessions, 'member-session', 'member-sessions')} of stock bars ${tally.memberSessions === 1 ? 'isn’t' : 'aren’t'} in the lake yet, across ${tally.lacking} of ${plural(tally.members, 'member', 'members')}.`,
    why: 'A run fetches each one from the provider, about one vendor call per member-session, so a cold universe is slow.',
    next: 'Capture each member’s sessions first, below.',
  };
}
