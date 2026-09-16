import { describe, expect, it } from 'vitest';
import { alpacaClerkMatchesAccount, sameAlpacaAccount } from './alpaca-account-identity';

describe('Alpaca account identity at the public route boundary', () => {
  it.each([
    ['PA9', 'pa9', true],
    ['PA9', 'PA10', false],
    ['shadow:PA9', 'PA9', false],
    ['PA9', null, false],
  ] as const)('matches %s only to the same external account %s', (reported, routed, expected) => {
    expect(sameAlpacaAccount(reported, routed)).toBe(expected);
  });

  it.each([
    ['shadow:PA9', 'shadow', true],
    ['shadow:PA10', 'shadow', false],
    ['PA9', 'shadow', false],
    ['shadow:PA9', 'real_live', false],
    ['PA9', 'real_paper', true],
  ] as const)('matches custody %s under %s to its external account', (accountId, authorityKind, expected) => {
    expect(alpacaClerkMatchesAccount({ account_id: accountId, authority_kind: authorityKind }, 'pa9')).toBe(expected);
  });
});
