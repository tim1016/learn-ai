import { describe, expect, it } from 'vitest';

import { buildManualOrderTicketNavigation } from './manual-order-navigation';

describe('buildManualOrderTicketNavigation', () => {
  it('preserves the routed Clerk and account instead of returning to the broker directory', () => {
    const navigation = buildManualOrderTicketNavigation({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      routeAccountId: 'PA-1',
      accountId: 'PA-1',
      symbol: 'SPY',
    });

    expect(navigation.commands).toEqual([
      '/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA-1',
    ]);
    expect(navigation.queryParams).toMatchObject({
      order: 'new', accountId: 'PA-1', symbol: 'SPY',
    });
  });

  it('opens the workspace the bot page is routed under, not the broker spelling (H20)', () => {
    const navigation = buildManualOrderTicketNavigation({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      routeAccountId: 'pa3kwxu1c4c3',
      accountId: 'PA3KWXU1C4C3',
      symbol: 'SPY',
    });

    expect(navigation.commands).toEqual([
      '/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'pa3kwxu1c4c3',
    ]);
    expect(navigation.queryParams).toMatchObject({ accountId: 'PA3KWXU1C4C3' });
  });
});
