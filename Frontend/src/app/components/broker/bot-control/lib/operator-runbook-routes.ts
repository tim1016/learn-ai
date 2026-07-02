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

export function resolveOperatorRunbookRoute(
  slug: string,
  instanceId: string | null = null,
): OperatorRunbookRoute | null {
  if ((slug === 'watchdog-halt' || slug === 'runtime-freshness') && instanceId) {
    return { commands: ['/broker/bots', instanceId] };
  }
  return RUNBOOK_ROUTES[slug] ?? null;
}
