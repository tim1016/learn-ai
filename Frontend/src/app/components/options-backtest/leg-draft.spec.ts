import { describe, expect, it } from 'vitest';

import {
  builderProblems,
  contractLine,
  copyOfLeg,
  legFromBuilder,
  newLegBuilder,
  nextLegId,
  withLegAdded,
  withLegCopied,
  withLegRemoved,
  type LegBuilder,
} from './leg-draft';
import type { SetupLimits } from './options-backtest.types';
import { newSetupDraft } from './setup-draft';
import { SERVED_LIMITS, SESSION_CLOCK } from './testing/fixtures';

const builder = (changes: Partial<LegBuilder>): LegBuilder => ({ ...newLegBuilder(), ...changes });
const problemsOf = (value: LegBuilder, limits: SetupLimits | null = SERVED_LIMITS) => builderProblems(value, limits);

describe('leg ids', () => {
  it('are the leg right, then numbered, so a straddle has the ids the example has', () => {
    expect(nextLegId('call', [])).toBe('call');
    expect(nextLegId('put', ['call'])).toBe('put');
    expect(nextLegId('call', ['call'])).toBe('call_2');
    expect(nextLegId('call', ['call', 'call_2'])).toBe('call_3');
  });

  it('fill the first gap, and are always valid ids', () => {
    expect(nextLegId('put', ['put', 'put_3'])).toBe('put_2');
    expect(nextLegId('call', Array.from({ length: 7 }, (_, i) => (i === 0 ? 'call' : `call_${i + 1}`)))).toMatch(/^[a-z0-9_]{1,32}$/);
  });

  it('are new for a copy, which keeps everything else', () => {
    const leg = { ...legFromBuilder(builder({ contracts: '3' }), []), stopLossOn: true, stopLossValue: '25' };

    const copy = copyOfLeg(leg, ['call']);

    expect(copy).toEqual({ ...leg, id: 'call_2' });
  });
});

describe('the leg builder', () => {
  it('starts as one at-the-money call on the weekly expiry, and can be added', () => {
    expect(problemsOf(newLegBuilder()).size).toBe(0);
    expect(legFromBuilder(newLegBuilder(), [])).toMatchObject({ id: 'call', right: 'call', contracts: 1, expiryKind: 'this_week', strikeKind: 'atm' });
  });

  it('adds a leg with every rule off', () => {
    expect(legFromBuilder(newLegBuilder(), [])).toMatchObject({ stopLossOn: false, momentumOn: false, reentryOn: false });
  });

  it('carries the days and the steps only when their rules are chosen', () => {
    const plain = legFromBuilder(builder({ expiryDays: '9', strikeSteps: '4' }), []);
    const chosen = legFromBuilder(builder({ expiryKind: 'min_days', expiryDays: '9', strikeKind: 'atm_offset', strikeSteps: '-4' }), []);

    expect(plain).toMatchObject({ expiryDays: 0, strikeSteps: 0 });
    expect(chosen).toMatchObject({ expiryKind: 'min_days', expiryDays: 9, strikeKind: 'atm_offset', strikeSteps: -4 });
  });

  it.each([['0'], ['1001'], ['1.5'], [''], ['abc'], ['-1']])('does not take %s contracts', (contracts) => {
    expect([...problemsOf(builder({ contracts })).keys()]).toEqual(['contracts']);
  });

  it.each([['1'], ['1000'], [' 7 ']])('takes %s contracts', (contracts) => {
    expect(problemsOf(builder({ contracts })).size).toBe(0);
  });

  it.each([['-1'], ['731'], [''], ['2.5']])('does not take %s days out', (expiryDays) => {
    expect([...problemsOf(builder({ expiryKind: 'min_days', expiryDays })).keys()]).toEqual(['expiryDays']);
  });

  it.each([['0'], ['730']])('takes %s days out', (expiryDays) => {
    expect(problemsOf(builder({ expiryKind: 'min_days', expiryDays })).size).toBe(0);
  });

  it('does not read the days while the expiry is another rule', () => {
    expect(problemsOf(builder({ expiryKind: 'same_day', expiryDays: 'junk' })).size).toBe(0);
  });

  it.each([['0'], ['51'], ['-51'], [''], ['1.5']])('does not take %s steps from the money', (strikeSteps) => {
    expect([...problemsOf(builder({ strikeKind: 'atm_offset', strikeSteps })).keys()]).toEqual(['strikeSteps']);
  });

  it.each([['1'], ['-1'], ['50'], ['-50']])('takes %s steps from the money', (strikeSteps) => {
    expect(problemsOf(builder({ strikeKind: 'atm_offset', strikeSteps })).size).toBe(0);
  });

  it('takes its ranges from the limits the server serves, so a changed limit moves them', () => {
    const tight: SetupLimits = { ...SERVED_LIMITS, max_contracts_per_leg: 5, max_expiry_days: 10, max_strike_steps: 3 };

    expect([...builderProblems(builder({ contracts: '6' }), tight).keys()]).toEqual(['contracts']);
    expect(builderProblems(builder({ contracts: '5' }), tight).size).toBe(0);
    expect([...builderProblems(builder({ expiryKind: 'min_days', expiryDays: '11' }), tight).keys()]).toEqual(['expiryDays']);
    expect(builderProblems(builder({ expiryKind: 'min_days', expiryDays: '10' }), tight).size).toBe(0);
    expect([...builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '-4' }), tight).keys()]).toEqual(['strikeSteps']);
    expect(builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '-3' }), tight).size).toBe(0);
  });

  it('says the served range in the sentence that refuses a number', () => {
    const tight: SetupLimits = { ...SERVED_LIMITS, max_contracts_per_leg: 5, max_expiry_days: 10, max_strike_steps: 3 };

    expect(builderProblems(builder({ contracts: '6' }), tight).get('contracts')).toContain('1 to 5');
    expect(builderProblems(builder({ expiryKind: 'min_days', expiryDays: '11' }), tight).get('expiryDays')).toContain('0 to 10');
    expect(builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '4' }), tight).get('strikeSteps')).toContain('-3 to 3');
  });

  it('checks only that a number is a whole number while the limits are not known, and leaves the range to the server', () => {
    expect(builderProblems(builder({ contracts: '5000' }), null).size).toBe(0);
    expect(builderProblems(builder({ expiryKind: 'min_days', expiryDays: '9999' }), null).size).toBe(0);
    expect(builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '-999' }), null).size).toBe(0);
    expect([...builderProblems(builder({ contracts: '1.5' }), null).keys()]).toEqual(['contracts']);
    expect([...builderProblems(builder({ contracts: '0' }), null).keys()]).toEqual(['contracts']);
    expect([...builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '0' }), null).keys()]).toEqual(['strikeSteps']);
  });
});

describe('the contract line', () => {
  it('reads left to right as the builder does', () => {
    expect(contractLine(legFromBuilder(newLegBuilder(), []))).toBe('Buy · 1 contract · Call · Weekly expiry · At the money');
  });

  it('says days and steps with their signs as words', () => {
    const leg = legFromBuilder(
      builder({ right: 'put', contracts: '2', expiryKind: 'min_days', expiryDays: '1', strikeKind: 'atm_offset', strikeSteps: '-3' }),
      [],
    );

    expect(contractLine(leg)).toBe('Buy · 2 contracts · Put · Expires at least 1 day out · 3 strikes below the money');
    expect(contractLine({ ...leg, expiryKind: 'same_day', strikeSteps: 1 })).toBe('Buy · 2 contracts · Put · Expires the same day · 1 strike above the money');
  });

  it('says Sell for a sold leg and reads as a bought one otherwise', () => {
    const sold = legFromBuilder(builder({ side: 'sell', right: 'put', contracts: '2' }), []);

    expect(contractLine(sold)).toBe('Sell · 2 contracts · Put · Weekly expiry · At the money');
    expect(contractLine({ ...sold, side: 'buy' })).toBe('Buy · 2 contracts · Put · Weekly expiry · At the money');
  });
});

describe('a leg’s side', () => {
  it('is Buy in a new builder, and the leg it makes carries the side chosen', () => {
    expect(newLegBuilder().side).toBe('buy');
    expect(legFromBuilder(newLegBuilder(), []).side).toBe('buy');
    expect(legFromBuilder(builder({ side: 'sell' }), []).side).toBe('sell');
  });

  it('is part of the contract a leg keeps when it is copied', () => {
    const sold = legFromBuilder(builder({ side: 'sell' }), []);

    expect(copyOfLeg(sold, ['call'])).toEqual({ ...sold, id: 'call_2' });
  });

  it('asks nothing of the builder that a bought leg does not, and a sold leg is held to the same ranges', () => {
    expect(problemsOf(builder({ side: 'sell' })).size).toBe(0);
    expect([...problemsOf(builder({ side: 'sell', contracts: '0' })).keys()]).toEqual(['contracts']);
  });
});

describe('the legs of a setup', () => {
  const ids = (draft: { legs: { id: string }[] }) => draft.legs.map((leg) => leg.id);
  const base = newSetupDraft(SESSION_CLOCK);

  it('grow by the builder’s leg, with an id of their own', () => {
    const once = withLegAdded(base, newLegBuilder(), SERVED_LIMITS);
    const twice = withLegAdded(once, builder({ right: 'put' }), SERVED_LIMITS);
    const again = withLegAdded(twice, newLegBuilder(), SERVED_LIMITS);

    expect(ids(again)).toEqual(['call', 'put', 'call_2']);
  });

  it('stop growing at the most legs the server allows, by adding and by copying', () => {
    const limits: SetupLimits = { ...SERVED_LIMITS, max_legs: 3 };
    let draft = base;
    for (let count = 0; count < 6; count += 1) draft = withLegAdded(draft, newLegBuilder(), limits);

    expect(draft.legs).toHaveLength(3);
    expect(withLegCopied(draft, 'call', limits)).toBe(draft);
  });

  it('go on growing past eight while the limits are not known: the server judges', () => {
    let draft = base;
    for (let count = 0; count < 10; count += 1) draft = withLegAdded(draft, newLegBuilder(), null);

    expect(draft.legs).toHaveLength(10);
    expect(withLegCopied(draft, 'call', null).legs).toHaveLength(11);
  });

  it('take a copy just after the leg it came from', () => {
    const draft = withLegAdded(withLegAdded(base, newLegBuilder(), SERVED_LIMITS), builder({ right: 'put' }), SERVED_LIMITS);

    expect(ids(withLegCopied(draft, 'call', SERVED_LIMITS))).toEqual(['call', 'call_2', 'put']);
  });

  it('leave a copy of a leg that is not there as it was', () => {
    expect(withLegCopied(base, 'nope', SERVED_LIMITS)).toBe(base);
  });

  it('lose a leg by its id, and the others keep theirs', () => {
    const draft = withLegAdded(withLegAdded(base, newLegBuilder(), SERVED_LIMITS), builder({ right: 'put' }), SERVED_LIMITS);

    expect(ids(withLegRemoved(draft, 'call'))).toEqual(['put']);
  });
});
