export interface OperatorRunbookRoute {
  readonly commands: readonly string[];
}

const RUNBOOK_ROUTES: Readonly<Record<string, OperatorRunbookRoute>> = {
  'broker-reconnect': { commands: ['/broker/account-monitor'] },
  'cross-client-execution': { commands: ['/broker/reconciliation'] },
  'live-trade-reconciliation': { commands: ['/broker/reconciliation'] },
  'broker-instance-operator-surface': { commands: ['/broker/reconciliation'] },
  'watchdog-halt': { commands: ['/broker/bots'] },
  'runtime-freshness': { commands: ['/broker/bots'] },
};

// Slugs whose runbook is "about this bot" — the resolver sends them to the
// instance's own control page (`/broker/bots/:id`). Invoked FROM that page they
// would navigate to self (a no-op), so the Verdict Card opens the why-drawer
// instead. See docs/superpowers/specs/2026-07-08-bot-control-verdict-card-design.md.
const INSTANCE_PAGE_RUNBOOK_SLUGS: ReadonlySet<string> = new Set([
  'watchdog-halt',
  'runtime-freshness',
]);

/** True when the runbook slug resolves to the bot's own control page. */
export function runbookOpensInstancePage(slug: string): boolean {
  return INSTANCE_PAGE_RUNBOOK_SLUGS.has(slug);
}

export function resolveOperatorRunbookRoute(
  slug: string,
  instanceId: string | null = null,
): OperatorRunbookRoute | null {
  if (runbookOpensInstancePage(slug) && instanceId) {
    return { commands: ['/broker/bots', instanceId] };
  }
  return RUNBOOK_ROUTES[slug] ?? null;
}
