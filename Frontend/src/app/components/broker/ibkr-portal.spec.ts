import { describe, expect, it } from 'vitest';

import { IBKR_PORTAL } from './ibkr-portal';

describe('IBKR_PORTAL', () => {
  it('ACCOUNT_MANAGEMENT_URL points at an https IBKR domain (regression net)', () => {
    const url = IBKR_PORTAL.ACCOUNT_MANAGEMENT_URL;
    const parsed = new URL(url);
    expect(parsed.protocol).toBe('https:');
    expect(parsed.hostname).toMatch(/interactivebrokers\.com$/);
  });
});
