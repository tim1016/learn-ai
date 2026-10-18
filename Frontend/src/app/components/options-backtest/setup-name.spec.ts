import { describe, expect, it } from 'vitest';

import { draftFromSetup } from './setup-draft';
import { generatedSetupName } from './setup-name';
import { exampleBody, SESSION_CLOCK } from './testing/fixtures';

describe('generated setup names', () => {
  it('describes ticker, ordered legs, strike direction and saved timing within the name contract', () => {
    const draft = draftFromSetup(exampleBody(), SESSION_CLOCK);
    draft.legs[1] = { ...draft.legs[1], side: 'sell', strikeKind: 'atm_offset', strikeSteps: -2 };
    const name = generatedSetupName(draft);
    expect(name).toContain('SPY, Buy 1 ATM call and Sell 1 ATM minus 2 put');
    expect(name).toContain('ET');
    expect(name).toMatch(/^[A-Za-z0-9][A-Za-z0-9 _.,()-]*$/);
    expect(name.length).toBeLessThanOrEqual(80);
    expect(generatedSetupName({ ...draft, legs: [...draft.legs, ...draft.legs, ...draft.legs] })).toContain('5 more legs');
  });
});
