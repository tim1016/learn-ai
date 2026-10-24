import { describe, expect, it } from 'vitest';

import { isOptionsSetupId, optionsDeployHandoffQuery } from './options-setup-handoff';

describe('isOptionsSetupId', () => {
  // The server's own pattern for a Golden Options id: both lengths it writes.
  it.each([`ogs_${'a'.repeat(32)}`, `ogs_${'0123456789abcdef'.repeat(2)}${'b'.repeat(8)}`])('takes %s as an approved options version', (id) => {
    expect(isOptionsSetupId(id)).toBe(true);
  });

  // A value the server would not read is never read here, so a different version cannot answer for it.
  it.each([
    null,
    '',
    `gq-${'a'.repeat(32)}`,
    `ogs_${'a'.repeat(31)}`,
    `ogs_${'a'.repeat(33)}`,
    `ogs_${'A'.repeat(32)}`,
    `ogs_${'a'.repeat(32)}/../accounts`,
    ` ogs_${'a'.repeat(32)}`,
  ])('refuses %s', (id) => {
    expect(isOptionsSetupId(id)).toBe(false);
  });
});

describe('optionsDeployHandoffQuery', () => {
  it('carries the version by its own id under the key options Deploy reads, and no other key', () => {
    expect(optionsDeployHandoffQuery(`ogs_${'a'.repeat(32)}`)).toEqual({ options_setup: `ogs_${'a'.repeat(32)}` });
  });
});
