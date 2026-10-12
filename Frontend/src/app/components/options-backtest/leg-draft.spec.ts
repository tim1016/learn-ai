import { describe, expect, it } from 'vitest';

import {
  builderProblems,
  contractLine,
  copyOfLeg,
  legFromBuilder,
  MAX_LEGS,
  newLegBuilder,
  nextLegId,
  withLegAdded,
  withLegCopied,
  withLegRemoved,
  type LegBuilder,
} from './leg-draft';
import { newSetupDraft } from './setup-draft';
import { SESSION_CLOCK } from './testing/fixtures';

const builder = (changes: Partial<LegBuilder>): LegBuilder => ({ ...newLegBuilder(), ...changes });

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
    expect(builderProblems(newLegBuilder()).size).toBe(0);
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
    expect([...builderProblems(builder({ contracts })).keys()]).toEqual(['contracts']);
  });

  it.each([['1'], ['1000'], [' 7 ']])('takes %s contracts', (contracts) => {
    expect(builderProblems(builder({ contracts })).size).toBe(0);
  });

  it.each([['-1'], ['731'], [''], ['2.5']])('does not take %s days out', (expiryDays) => {
    expect([...builderProblems(builder({ expiryKind: 'min_days', expiryDays })).keys()]).toEqual(['expiryDays']);
  });

  it.each([['0'], ['730']])('takes %s days out', (expiryDays) => {
    expect(builderProblems(builder({ expiryKind: 'min_days', expiryDays })).size).toBe(0);
  });

  it('does not read the days while the expiry is another rule', () => {
    expect(builderProblems(builder({ expiryKind: 'same_day', expiryDays: 'junk' })).size).toBe(0);
  });

  it.each([['0'], ['51'], ['-51'], [''], ['1.5']])('does not take %s steps from the money', (strikeSteps) => {
    expect([...builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps })).keys()]).toEqual(['strikeSteps']);
  });

  it.each([['1'], ['-1'], ['50'], ['-50']])('takes %s steps from the money', (strikeSteps) => {
    expect(builderProblems(builder({ strikeKind: 'atm_offset', strikeSteps })).size).toBe(0);
  });

  it('holds at most eight legs', () => {
    expect(MAX_LEGS).toBe(8);
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
});

describe('the legs of a setup', () => {
  const ids = (draft: { legs: { id: string }[] }) => draft.legs.map((leg) => leg.id);
  const base = newSetupDraft(SESSION_CLOCK);

  it('grow by the builder’s leg, with an id of their own', () => {
    const once = withLegAdded(base, newLegBuilder());
    const twice = withLegAdded(once, builder({ right: 'put' }));
    const again = withLegAdded(twice, newLegBuilder());

    expect(ids(again)).toEqual(['call', 'put', 'call_2']);
  });

  it('stop growing at eight, by adding and by copying', () => {
    let draft = base;
    for (let count = 0; count < MAX_LEGS + 3; count += 1) draft = withLegAdded(draft, newLegBuilder());

    expect(draft.legs).toHaveLength(MAX_LEGS);
    expect(withLegCopied(draft, 'call')).toBe(draft);
  });

  it('take a copy just after the leg it came from', () => {
    const draft = withLegAdded(withLegAdded(base, newLegBuilder()), builder({ right: 'put' }));

    expect(ids(withLegCopied(draft, 'call'))).toEqual(['call', 'call_2', 'put']);
  });

  it('leave a copy of a leg that is not there as it was', () => {
    expect(withLegCopied(base, 'nope')).toBe(base);
  });

  it('lose a leg by its id, and the others keep theirs', () => {
    const draft = withLegAdded(withLegAdded(base, newLegBuilder()), builder({ right: 'put' }));

    expect(ids(withLegRemoved(draft, 'call'))).toEqual(['put']);
  });
});
