import { describe, expect, it } from 'vitest';

import catalog from './fleet-operation-catalog.snapshot.json';
import { operationUrl, type OperationId } from './operation-url';

const TARGET = { broker: 'alpaca', clerkId: 'clrk_1', accountId: 'PA9' };

describe('operationUrl', () => {
  it.each([
    ['builds the declared path for a lane-scoped read', 'account_read', TARGET,
      '/api/brokers/alpaca/clerks/clrk_1/account'],
    ['builds a clerk-scoped IBKR market-data URL for the selected symbol', 'ibkr_option_chain_stream',
      { ...TARGET, symbol: 'SPY' }, '/api/brokers/alpaca/clerks/clrk_1/market-data/ibkr/option-chain/SPY'],
    ['builds the declared path for an account-scoped read', 'bots_catalog_read', TARGET,
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/bots/catalog'],
    // manual_order_cancel declares {order_ref:path}; encoding its slashes would
    // under-match the route the coordinator mounts.
    ['does not percent-encode a :path parameter', 'manual_order_cancel', { ...TARGET, orderRef: 'alpaca/abc-123' },
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/manual-orders/alpaca/abc-123/cancel'],
    // The negative case for the row above: a non-`:path` parameter's
    // slash must still be escaped, or this test cannot tell "operationUrl
    // leaves slashes alone" apart from "operationUrl leaves slashes alone
    // only for order_ref specifically".
    ['percent-encodes an ordinary path parameter that is not a :path segment', 'manual_order_ticket_read',
      { ...TARGET, ticketId: 'a/b' }, '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/manual-order-tickets/a%2Fb'],
    ['builds a path needing both an account and a second parameter', 'bot_panel_read', { ...TARGET, sid: 'sid-7' },
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/bots/sid-7/panel'],
  ] as const)('resolves the exact catalog URL: %s', (_name, operationId, target, expected) => {
    expect(operationUrl(operationId, target)).toBe(expected);
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
      programKey: 'program-1',
      submissionKey: 'submission-1',
      symbol: 'SPY',
    };
    for (const operationId of Object.keys(catalog.operations) as OperationId[]) {
      expect(() => operationUrl(operationId, fullTarget)).not.toThrow();
    }
  });
});
