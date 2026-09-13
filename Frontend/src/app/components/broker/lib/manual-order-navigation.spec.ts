import { describe, expect, it } from 'vitest';

import { buildManualOrderTicketNavigation } from './manual-order-navigation';

describe('buildManualOrderTicketNavigation', () => {
  it('preserves the routed Clerk and account instead of returning to the broker directory', () => {
    const navigation = buildManualOrderTicketNavigation(
      'alpaca',
      'clrk_spec',
      'PA-1',
      'SPY',
    );

    expect(navigation.commands).toEqual([
      '/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA-1',
    ]);
    expect(navigation.queryParams).toMatchObject({
      order: 'new', accountId: 'PA-1', symbol: 'SPY',
    });
  });
});
