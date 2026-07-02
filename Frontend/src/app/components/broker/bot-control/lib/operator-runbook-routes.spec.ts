import { describe, expect, it } from 'vitest';

import { resolveOperatorRunbookRoute } from './operator-runbook-routes';

describe('resolveOperatorRunbookRoute', () => {
  it('maps known runbook slugs to explicit operator surfaces', () => {
    expect(resolveOperatorRunbookRoute('broker-reconnect')).toEqual({
      commands: ['/broker/account-monitor'],
    });
    expect(resolveOperatorRunbookRoute('cross-client-execution')).toEqual({
      commands: ['/broker/reconciliation'],
    });
  });

  it('keeps bot-scoped runbooks on the current bot when an instance id is available', () => {
    expect(resolveOperatorRunbookRoute('watchdog-halt', 'DEPVALJUL1')).toEqual({
      commands: ['/broker/bots', 'DEPVALJUL1'],
    });
  });

  it('fails closed for unknown backend slugs', () => {
    expect(resolveOperatorRunbookRoute('invented-runbook')).toBeNull();
  });
});
