import { expect } from '@playwright/test';

/** The shell's own per-lane polls. They fire on every route, whatever page is
 * open: the trust-anchor verdict (#2161), and the Clerk status the top bar's
 * IBKR data pill reads from every lane (#2810), since one IB Gateway feeds
 * them all. */
const SHELL_POLL = /^GET \/api\/brokers\/alpaca\/clerks\/[^/]+\/(?:live-verdict|clerk\/status)(?:\?.*)?$/;

/** Drop the shell's per-lane polls from a request ledger: they prove the
 * shell is alive, not that a surface reached into a lane. A workspace reads
 * its own lane's Clerk status at the same URL as the pill, so a ledger with a
 * workspace open also owes `expectStatusReadOnlyFromOwnLane`. */
export const withoutShellPolls = (requests: readonly string[]): string[] =>
  requests.filter((entry) => !SHELL_POLL.test(entry));

const statusReads = (requests: readonly string[], clerkId: string): number =>
  requests.filter((entry) => entry === `GET /api/brokers/alpaca/clerks/${clerkId}/clerk/status`).length;

/**
 * The open workspace read its own lane's Clerk status and no other lane's.
 *
 * The pill and a workspace read the same URL, so the ledger cannot say which
 * of them sent a read. The counts can: the pill reads every lane once a tick
 * and the workspace reads only its own lane on top, so the other lane stays
 * behind. A workspace read sent to the other lane, in place of its own or as
 * well, closes that gap. Polled, because a tick's reads land one at a time.
 */
export async function expectStatusReadOnlyFromOwnLane(
  ledger: () => readonly string[],
  lanes: { readonly own: string; readonly other: string },
): Promise<void> {
  await expect
    .poll(() => statusReads(ledger(), lanes.other) < statusReads(ledger(), lanes.own))
    .toBe(true);
}
