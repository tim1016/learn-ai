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
  it('are the leg right, then numbered, fill the first gap, are always valid ids, and are new for a copy, which keeps everything else', () => {
    expect.soft(nextLegId('call', []), 'are the leg right, then numbered, so a straddle has the ids the example has (a first call)').toBe('call');
    expect.soft(nextLegId('put', ['call']), 'are the leg right, then numbered (a put beside a call)').toBe('put');
    expect.soft(nextLegId('call', ['call']), 'are the leg right, then numbered (a second call)').toBe('call_2');
    expect.soft(nextLegId('call', ['call', 'call_2']), 'are the leg right, then numbered (a third call)').toBe('call_3');

    expect.soft(nextLegId('put', ['put', 'put_3']), 'fill the first gap').toBe('put_2');
    expect.soft(nextLegId('call', Array.from({ length: 7 }, (_, i) => (i === 0 ? 'call' : `call_${i + 1}`))), 'are always valid ids').toMatch(/^[a-z0-9_]{1,32}$/);

    const leg = { ...legFromBuilder(builder({ contracts: '3' }), []), stopLossOn: true, stopLossValue: '25' };

    const copy = copyOfLeg(leg, ['call']);

    expect.soft(copy, 'are new for a copy, which keeps everything else').toEqual({ ...leg, id: 'call_2' });
  });
});

describe('the leg builder', () => {
  it('starts as one at-the-money call on the weekly expiry and can be added, adds a leg with every rule off, and carries the days and the steps only when their rules are chosen', () => {
    const plain = legFromBuilder(builder({ expiryDays: '9', strikeSteps: '4' }), []);
    const chosen = legFromBuilder(builder({ expiryKind: 'min_days', expiryDays: '9', strikeKind: 'atm_offset', strikeSteps: '-4' }), []);

    expect.soft(problemsOf(newLegBuilder()).size, 'starts as one at-the-money call on the weekly expiry (it has no problem)').toBe(0);
    expect.soft(legFromBuilder(newLegBuilder(), []), 'starts as one at-the-money call on the weekly expiry, and can be added').toMatchObject({ id: 'call', right: 'call', contracts: 1, expiryKind: 'this_week', strikeKind: 'atm' });
    expect.soft(legFromBuilder(newLegBuilder(), []), 'adds a leg with every rule off').toMatchObject({ stopLossOn: false, momentumOn: false, reentryOn: false });
    expect.soft(plain, 'carries the days and the steps only when their rules are chosen (rules not chosen)').toMatchObject({ expiryDays: 0, strikeSteps: 0 });
    expect.soft(chosen, 'carries the days and the steps only when their rules are chosen (rules chosen)').toMatchObject({ expiryKind: 'min_days', expiryDays: 9, strikeKind: 'atm_offset', strikeSteps: -4 });
  });

  // The problems a builder holds: the field each refused value is named under, or none for a value that is taken.
  it.each<[string, Partial<LegBuilder>, string[]]>([
    ['does not take 0 contracts', { contracts: '0' }, ['contracts']],
    ['does not take 1001 contracts', { contracts: '1001' }, ['contracts']],
    ['does not take 1.5 contracts', { contracts: '1.5' }, ['contracts']],
    ['does not take a blank for contracts', { contracts: '' }, ['contracts']],
    ['does not take abc contracts', { contracts: 'abc' }, ['contracts']],
    ['does not take -1 contracts', { contracts: '-1' }, ['contracts']],
    ['takes 1 contracts', { contracts: '1' }, []],
    ['takes 1000 contracts', { contracts: '1000' }, []],
    ['takes " 7 " contracts, spaces around', { contracts: ' 7 ' }, []],
    ['does not take -1 days out', { expiryKind: 'min_days', expiryDays: '-1' }, ['expiryDays']],
    ['does not take 731 days out', { expiryKind: 'min_days', expiryDays: '731' }, ['expiryDays']],
    ['does not take a blank for days out', { expiryKind: 'min_days', expiryDays: '' }, ['expiryDays']],
    ['does not take 2.5 days out', { expiryKind: 'min_days', expiryDays: '2.5' }, ['expiryDays']],
    ['takes 0 days out', { expiryKind: 'min_days', expiryDays: '0' }, []],
    ['takes 730 days out', { expiryKind: 'min_days', expiryDays: '730' }, []],
    ['does not read the days while the expiry is another rule', { expiryKind: 'same_day', expiryDays: 'junk' }, []],
    ['does not take 0 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '0' }, ['strikeSteps']],
    ['does not take 51 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '51' }, ['strikeSteps']],
    ['does not take -51 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '-51' }, ['strikeSteps']],
    ['does not take a blank for steps from the money', { strikeKind: 'atm_offset', strikeSteps: '' }, ['strikeSteps']],
    ['does not take 1.5 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '1.5' }, ['strikeSteps']],
    ['takes 1 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '1' }, []],
    ['takes -1 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '-1' }, []],
    ['takes 50 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '50' }, []],
    ['takes -50 steps from the money', { strikeKind: 'atm_offset', strikeSteps: '-50' }, []],
  ])('%s', (_name, changes, problems) => {
    expect([...problemsOf(builder(changes)).keys()]).toEqual(problems);
  });

  it('takes its ranges from the limits the server serves, so a changed limit moves them, and says the served range in the sentence that refuses a number', () => {
    const tight: SetupLimits = { ...SERVED_LIMITS, max_contracts_per_leg: 5, max_expiry_days: 10, max_strike_steps: 3 };

    expect.soft([...builderProblems(builder({ contracts: '6' }), tight).keys()], 'takes its ranges from the limits the server serves, so a changed limit moves them (contracts over the limit)').toEqual(['contracts']);
    expect.soft(builderProblems(builder({ contracts: '5' }), tight).size, 'takes its ranges from the limits the server serves, so a changed limit moves them (contracts at the limit)').toBe(0);
    expect.soft([...builderProblems(builder({ expiryKind: 'min_days', expiryDays: '11' }), tight).keys()], 'takes its ranges from the limits the server serves, so a changed limit moves them (days over the limit)').toEqual(['expiryDays']);
    expect.soft(builderProblems(builder({ expiryKind: 'min_days', expiryDays: '10' }), tight).size, 'takes its ranges from the limits the server serves, so a changed limit moves them (days at the limit)').toBe(0);
    expect.soft([...builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '-4' }), tight).keys()], 'takes its ranges from the limits the server serves, so a changed limit moves them (steps over the limit)').toEqual(['strikeSteps']);
    expect.soft(builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '-3' }), tight).size, 'takes its ranges from the limits the server serves, so a changed limit moves them (steps at the limit)').toBe(0);

    expect.soft(builderProblems(builder({ contracts: '6' }), tight).get('contracts'), 'says the served range in the sentence that refuses a number (contracts)').toContain('1 to 5');
    expect.soft(builderProblems(builder({ expiryKind: 'min_days', expiryDays: '11' }), tight).get('expiryDays'), 'says the served range in the sentence that refuses a number (days)').toContain('0 to 10');
    expect.soft(builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps: '4' }), tight).get('strikeSteps'), 'says the served range in the sentence that refuses a number (steps)').toContain('-3 to 3');
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
  it('reads left to right as the builder does, says days and steps with their signs as words, and says Sell for a sold leg and reads as a bought one otherwise', () => {
    const leg = legFromBuilder(
      builder({ right: 'put', contracts: '2', expiryKind: 'min_days', expiryDays: '1', strikeKind: 'atm_offset', strikeSteps: '-3' }),
      [],
    );
    const sold = legFromBuilder(builder({ side: 'sell', right: 'put', contracts: '2' }), []);

    expect.soft(contractLine(legFromBuilder(newLegBuilder(), [])), 'reads left to right as the builder does').toBe('Buy · 1 contract · Call · Weekly expiry · At the money');

    expect.soft(contractLine(leg), 'says days and steps with their signs as words (days out, strikes below)').toBe('Buy · 2 contracts · Put · Expires at least 1 day out · 3 strikes below the money');
    expect.soft(contractLine({ ...leg, expiryKind: 'same_day', strikeSteps: 1 }), 'says days and steps with their signs as words (same day, strike above)').toBe('Buy · 2 contracts · Put · Expires the same day · 1 strike above the money');

    expect.soft(contractLine(sold), 'says Sell for a sold leg').toBe('Sell · 2 contracts · Put · Weekly expiry · At the money');
    expect.soft(contractLine({ ...sold, side: 'buy' }), 'reads as a bought one otherwise').toBe('Buy · 2 contracts · Put · Weekly expiry · At the money');
  });
});

describe('a leg’s side', () => {
  it('is Buy in a new builder and the leg it makes carries the side chosen, is part of the contract a leg keeps when it is copied, and asks nothing of the builder that a bought leg does not', () => {
    const sold = legFromBuilder(builder({ side: 'sell' }), []);

    expect.soft(newLegBuilder().side, 'is Buy in a new builder').toBe('buy');
    expect.soft(legFromBuilder(newLegBuilder(), []).side, 'is Buy in the leg a new builder makes').toBe('buy');
    expect.soft(legFromBuilder(builder({ side: 'sell' }), []).side, 'the leg it makes carries the side chosen').toBe('sell');

    expect.soft(copyOfLeg(sold, ['call']), 'is part of the contract a leg keeps when it is copied').toEqual({ ...sold, id: 'call_2' });

    expect.soft(problemsOf(builder({ side: 'sell' })).size, 'asks nothing of the builder that a bought leg does not').toBe(0);
    expect.soft([...problemsOf(builder({ side: 'sell', contracts: '0' })).keys()], 'a sold leg is held to the same ranges').toEqual(['contracts']);
  });
});

describe('the legs of a setup', () => {
  const ids = (draft: { legs: { id: string }[] }) => draft.legs.map((leg) => leg.id);
  const base = newSetupDraft(SESSION_CLOCK);

  it('grow by the builder’s leg with an id of their own, stop growing at the most legs the server allows by adding and by copying, and go on growing past eight while the limits are not known', () => {
    const once = withLegAdded(base, newLegBuilder(), SERVED_LIMITS);
    const twice = withLegAdded(once, builder({ right: 'put' }), SERVED_LIMITS);
    const again = withLegAdded(twice, newLegBuilder(), SERVED_LIMITS);

    expect.soft(ids(again), 'grow by the builder’s leg, with an id of their own').toEqual(['call', 'put', 'call_2']);

    const limits: SetupLimits = { ...SERVED_LIMITS, max_legs: 3 };
    let capped = base;
    for (let count = 0; count < 6; count += 1) capped = withLegAdded(capped, newLegBuilder(), limits);

    expect.soft(capped.legs, 'stop growing at the most legs the server allows (adding)').toHaveLength(3);
    expect.soft(withLegCopied(capped, 'call', limits), 'stop growing at the most legs the server allows (copying)').toBe(capped);

    let unlimited = base;
    for (let count = 0; count < 10; count += 1) unlimited = withLegAdded(unlimited, newLegBuilder(), null);

    expect.soft(unlimited.legs, 'go on growing past eight while the limits are not known: the server judges (adding)').toHaveLength(10);
    expect.soft(withLegCopied(unlimited, 'call', null).legs, 'go on growing past eight while the limits are not known: the server judges (copying)').toHaveLength(11);
  });

  it('take a copy just after the leg it came from, leave a copy of a leg that is not there as it was, and lose a leg by its id while the others keep theirs', () => {
    const draft = withLegAdded(withLegAdded(base, newLegBuilder(), SERVED_LIMITS), builder({ right: 'put' }), SERVED_LIMITS);

    expect.soft(ids(withLegCopied(draft, 'call', SERVED_LIMITS)), 'take a copy just after the leg it came from').toEqual(['call', 'call_2', 'put']);
    expect.soft(withLegCopied(base, 'nope', SERVED_LIMITS), 'leave a copy of a leg that is not there as it was').toBe(base);
    expect.soft(ids(withLegRemoved(draft, 'call')), 'lose a leg by its id, and the others keep theirs').toEqual(['put']);
  });
});
