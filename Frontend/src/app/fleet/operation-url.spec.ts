import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import catalog from './fleet-operation-catalog.snapshot.json';
import { operationUrl, type OperationId } from './operation-url';

const TARGET = { broker: 'alpaca', clerkId: 'clrk_1', accountId: 'PA9' };

describe('operationUrl', () => {
  it('builds the declared path for a lane-scoped read', () => {
    expect(operationUrl('account_read', TARGET)).toBe('/api/brokers/alpaca/clerks/clrk_1/account');
  });

  it('builds the declared path for an account-scoped read', () => {
    expect(operationUrl('bots_catalog_read', TARGET)).toBe(
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/bots/catalog',
    );
  });

  it('refuses an operation the catalog does not declare', () => {
    expect(() => operationUrl('order_groups_read' as never, TARGET)).toThrow(/not a declared/i);
  });

  it('refuses an account-scoped operation with no account', () => {
    expect(() => operationUrl('bots_catalog_read', { broker: 'alpaca', clerkId: 'clrk_1' })).toThrow(
      /account/i,
    );
  });

  it('refuses an account-scoped operation whose account is blank', () => {
    // A distinct mutation from the "no account at all" case above: this
    // reddens only if the refusal checks for an empty string, not merely
    // for `undefined`/`null`.
    expect(() =>
      operationUrl('bots_catalog_read', { broker: 'alpaca', clerkId: 'clrk_1', accountId: '   ' }),
    ).toThrow(/account/i);
  });

  it('does not percent-encode a :path parameter', () => {
    // manual_order_cancel declares {order_ref:path}; encoding its slashes would
    // under-match the route the coordinator mounts.
    expect(operationUrl('manual_order_cancel', { ...TARGET, orderRef: 'alpaca/abc-123' })).toBe(
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/manual-orders/alpaca/abc-123/cancel',
    );
  });

  it('percent-encodes an ordinary path parameter that is not a :path segment', () => {
    // The negative case for the assertion above: a non-`:path` parameter's
    // slash must still be escaped, or this test cannot tell "operationUrl
    // leaves slashes alone" apart from "operationUrl leaves slashes alone
    // only for order_ref specifically".
    expect(
      operationUrl('manual_order_ticket_read', { ...TARGET, ticketId: 'a/b' }),
    ).toBe('/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/manual-order-tickets/a%2Fb');
  });

  it('builds a path needing both an account and a second parameter', () => {
    expect(operationUrl('bot_panel_read', { ...TARGET, sid: 'sid-7' })).toBe(
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/bots/sid-7/panel',
    );
  });

  it('declares a comfortably non-empty set of operations', () => {
    // Not pinned to the exact count (76 as of #2103): that number drifts as
    // the catalog grows, and a test asserting `toHaveLength(76)` forever
    // would need editing on every future addition anyway. The floor is what
    // makes this a canary that can actually fire — an accidentally emptied
    // or truncated snapshot still reddens here.
    expect(Object.keys(catalog.operations).length).toBeGreaterThanOrEqual(50);
  });

  it('every declared operation resolves without throwing, given the parameters its template needs', () => {
    // Assert the OTHER direction from the "refuses an undeclared operation"
    // test above: exhaustively prove every operation the catalog actually
    // lists is reachable, not just that a nonexistent one is refused. Feeds
    // every possible path-parameter field so each operation's own subset is
    // satisfied regardless of which ones its template needs.
    const fullTarget = {
      ...TARGET,
      sid: 'sid-1',
      ticketId: 'ticket-1',
      orderRef: 'order-1',
      profileId: 'profile-1',
      revision: 'rev-1',
      transactionId: 'txn-1',
      externalOrderId: 'ext-1',
      commandId: 'cmd-1',
      programKey: 'program-1',
    };
    for (const operationId of Object.keys(catalog.operations) as OperationId[]) {
      expect(() => operationUrl(operationId, fullTarget)).not.toThrow();
    }
  });

  it('no service reaches an unscoped broker path that the catalog does not declare', () => {
    // GET /api/brokers/{broker}/order-groups is mounted clerk-only and is not in
    // the catalog, so in fleet posture it 404s. Its one caller was dead (#2103
    // Task 11 deletes brokers.service.ts's listOrderGroups).
    const servicePath = join(__dirname, '..', 'services', 'brokers.service.ts');
    const source = readFileSync(servicePath, 'utf8');
    expect(source).not.toContain('order-groups');
  });
});
