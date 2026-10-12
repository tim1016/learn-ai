import { describe, expect, it } from 'vitest';

import { cleanMessage, describeDetail, FIELD, legKey, problemsFromDetails } from './setup-problems';

const at = (loc: (string | number)[], msg = 'bad') => ({ loc, msg });

describe('the server field errors', () => {
  it.each([
    [['body', 'underlying'], FIELD.underlying],
    [['body', 'entry', 'offset_ms'], FIELD.entry],
    [['body', 'exit', 'anchor'], FIELD.exit],
    [['body', 'no_reentry_after', 'offset_ms'], FIELD.noReentry],
    [['body', 'square_off'], FIELD.squareOff],
    [['body', 'slippage_percent'], FIELD.slippage],
    [['body', 'starting_cash_usd'], FIELD.cash],
    [['body', 'fees'], FIELD.fees],
    [['body', 'fees', 'per_contract_usd'], FIELD.perContractFee],
    [['body', 'legs'], FIELD.legs],
    [['path', 'name'], FIELD.name],
  ])('land at the field %j names', (loc, key) => {
    expect([...problemsFromDetails([at(loc)], ['call']).fields.keys()]).toEqual([key]);
  });

  it('land on the leg at that position, by its id', () => {
    const { fields } = problemsFromDetails([at(['body', 'legs', 1, 'contracts'], 'too many')], ['call', 'put']);

    expect(fields.get(legKey('put', 'contracts'))).toBe('too many');
  });

  it.each([
    [['legs', 0, 'stop_loss', 'value'], 'stopLossValue'],
    [['legs', 0, 'stop_loss'], 'stopLoss'],
    [['legs', 0, 'momentum', 'value'], 'momentumValue'],
    [['legs', 0, 'reentry_on_stop', 'count'], 'reentryCount'],
    [['legs', 0, 'reentry_on_stop', 'kind'], 'reentry'],
    [['legs', 0, 'expiry', 'min_days', 'days'], 'expiry'],
    [['legs', 0, 'strike', 'atm_offset', 'steps'], 'strike'],
    [['legs', 0], 'leg'],
    [['legs', 0, 'leg_id'], 'leg'],
  ] as const)('find the field of a leg through the union tag: %j', (inside, field) => {
    const { fields } = problemsFromDetails([at(['body', ...inside])], ['call']);

    expect([...fields.keys()]).toEqual([legKey('call', field)]);
  });

  it('go to the legs card when the position names no leg of the body that was sent', () => {
    expect([...problemsFromDetails([at(['body', 'legs', 5, 'contracts'])], ['call']).fields.keys()]).toEqual([FIELD.legs]);
  });

  it('are kept, not dropped, when they name no field the page has', () => {
    const { fields, other } = problemsFromDetails([at(['body'], 'Value error, every leg needs its own id'), at(['body', 'price_step_usd'], 'too small')], []);

    expect(fields.size).toBe(0);
    expect(other).toEqual(['every leg needs its own id', 'price_step_usd: too small']);
  });

  it('are joined when a field has more than one', () => {
    const { fields } = problemsFromDetails([at(['body', 'underlying'], 'first'), at(['body', 'underlying'], 'second')], []);

    expect(fields.get(FIELD.underlying)).toBe('first second');
  });
});

describe('messages', () => {
  it('lose the framework prefix of a rule the schema states', () => {
    expect(cleanMessage("Value error, leg 'call' re-enters on momentum")).toBe("leg 'call' re-enters on momentum");
    expect(cleanMessage('Input should be a valid integer')).toBe('Input should be a valid integer');
  });

  it('are listed with where they are, minus the body', () => {
    expect(describeDetail(at(['body', 'legs', 0, 'contracts'], 'too many'))).toBe('legs.0.contracts: too many');
    expect(describeDetail(at([], 'nope'))).toBe('nope');
  });
});
