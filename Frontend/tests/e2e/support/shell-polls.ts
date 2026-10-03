/** The shell's own per-lane polls. They fire on every route, whatever page is
 * open: the trust-anchor verdict (#2161), and the Clerk status the top bar's
 * IBKR data pill reads from every lane (#2810), since one IB Gateway feeds
 * them all. */
const SHELL_POLL = /^GET \/api\/brokers\/alpaca\/clerks\/[^/]+\/(?:live-verdict|clerk\/status)(?:\?.*)?$/;

/** Drop the shell's per-lane polls from a request ledger: they prove the
 * shell is alive, not that a surface reached into a lane. */
export const withoutShellPolls = (requests: readonly string[]): string[] =>
  requests.filter((entry) => !SHELL_POLL.test(entry));
