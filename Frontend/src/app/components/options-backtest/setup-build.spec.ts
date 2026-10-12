import { describe, expect, it } from 'vitest';

import { legFromBuilder, newLegBuilder } from './leg-draft';
import { buildSetup } from './setup-build';
import { draftFromSetup, newSetupDraft, type LegDraft, type SetupDraft } from './setup-draft';
import { formatClockText } from './session-time';
import { legKey } from './setup-problems';
import { exampleBody, minutes, SESSION_CLOCK, servedExamples } from './testing/fixtures';
import type { SetupRequest } from './options-backtest.types';

const { open_minute_et: OPEN, close_minute_et: CLOSE } = SESSION_CLOCK;

function built(draft: SetupDraft): SetupRequest {
  const result = buildSetup(draft, SESSION_CLOCK);
  if (!result.ok) throw new Error(`The draft should build: ${[...result.problems.values()].join(' / ')}`);
  return result.body;
}

function problemsOf(draft: SetupDraft): ReadonlyMap<string, string> {
  const result = buildSetup(draft, SESSION_CLOCK);
  if (result.ok) throw new Error('The draft should not build.');
  return result.problems;
}

/** The shipped example, put together the way an owner would: both legs bought at the money this week, every rule on. */
function exampleByHand(): SetupDraft {
  const withRules = (leg: LegDraft): LegDraft => ({
    ...leg,
    stopLossOn: true,
    stopLossValue: '25',
    momentumOn: true,
    momentumValue: '25',
    reentryOn: true,
  });
  const call = withRules(legFromBuilder({ ...newLegBuilder(), right: 'call' }, []));
  const put = withRules(legFromBuilder({ ...newLegBuilder(), right: 'put' }, [call.id]));
  return {
    ...newSetupDraft(SESSION_CLOCK),
    squareOff: 'complete',
    noReentryOn: true,
    noReentryClock: formatClockText(CLOSE - 60),
    entryClock: formatClockText(OPEN + 5),
    exitClock: formatClockText(CLOSE - 46),
    legs: [call, put],
  };
}

describe('building the shipped example by hand', () => {
  it('produces exactly the example JSON', () => {
    expect(built(exampleByHand())).toEqual(exampleBody());
  });

  it('produces the other example when the re-entry cutoff is the entry minute', () => {
    const draft = { ...exampleByHand(), noReentryClock: formatClockText(OPEN + 5) };

    expect(built(draft)).toEqual(exampleBody(minutes(5, 'open')));
  });
});

describe('a new setup', () => {
  it('starts on SPY with the times read from the clock and no legs', () => {
    const draft = newSetupDraft(SESSION_CLOCK);

    expect(draft).toMatchObject({
      underlying: 'SPY',
      entryClock: formatClockText(OPEN + 5),
      exitClock: formatClockText(CLOSE - 15),
      noReentryOn: false,
      squareOff: 'partial',
      slippage: '0.3',
      feeKind: 'not_charged',
      legs: [],
    });
  });

  it('has no times before the clock is read', () => {
    expect(newSetupDraft(null)).toMatchObject({ entryClock: '', exitClock: '', noReentryClock: '' });
  });

  it('builds a body the server can read, without legs, from its defaults', () => {
    const body = built(newSetupDraft(SESSION_CLOCK));

    expect(body).toMatchObject({ version: 1, entry: minutes(5, 'open'), exit: minutes(15, 'close'), legs: [], price_step_usd: '0.01' });
    expect('no_reentry_after' in body).toBe(false);
  });
});

describe('a switched-off rule', () => {
  const leg = (changes: Partial<LegDraft>): SetupDraft => ({
    ...newSetupDraft(SESSION_CLOCK),
    legs: [{ ...legFromBuilder(newLegBuilder(), []), ...changes }],
  });

  it('sends nothing, whatever its fields hold', () => {
    const body = built(leg({ stopLossValue: 'not a number', momentumValue: '', reentryCount: 'many' }));

    expect(body.legs[0]).toEqual({ leg_id: 'call', right: 'call', contracts: 1, expiry: { kind: 'this_week' }, strike: { kind: 'atm' } });
  });

  it('sends the stop loss when it is on', () => {
    expect(built(leg({ stopLossOn: true, stopLossKind: 'points', stopLossValue: ' 1.5 ' })).legs[0]).toMatchObject({ stop_loss: { kind: 'points', value: '1.5' } });
  });

  it('sends the momentum when it is on', () => {
    expect(built(leg({ momentumOn: true, momentumKind: 'points_down', momentumValue: '0.4' })).legs[0]).toMatchObject({ momentum: { kind: 'points_down', value: '0.4' } });
  });

  it('sends the re-entry, with its count as a number, when it is on', () => {
    expect(built(leg({ reentryOn: true, reentryKind: 'at_once', reentryCount: '3' })).legs[0]).toMatchObject({ reentry_on_stop: { kind: 'at_once', count: 3 } });
  });

  it('sends only the rule that is on, leaving the others off', () => {
    const sent = built(leg({ momentumOn: true, momentumValue: '10' })).legs[0];

    expect(sent).toHaveProperty('momentum');
    expect(sent).not.toHaveProperty('stop_loss');
    expect(sent).not.toHaveProperty('reentry_on_stop');
  });

  it('turns the "no re-entry after" time off the same way', () => {
    const on = built({ ...newSetupDraft(SESSION_CLOCK), noReentryOn: true });
    const off = built({ ...newSetupDraft(SESSION_CLOCK), noReentryOn: false, noReentryClock: 'garbage' });

    expect(on.no_reentry_after).toEqual(minutes(60, 'close'));
    expect(off).not.toHaveProperty('no_reentry_after');
  });
});

describe('a rule that is on but cannot be read', () => {
  const leg = (changes: Partial<LegDraft>): SetupDraft => ({
    ...newSetupDraft(SESSION_CLOCK),
    legs: [{ ...legFromBuilder(newLegBuilder(), []), ...changes }],
  });

  it.each([
    [{ stopLossOn: true, stopLossValue: '' }, 'stopLossValue'],
    [{ momentumOn: true, momentumValue: '2 percent' }, 'momentumValue'],
    [{ reentryOn: true, reentryCount: '1.5' }, 'reentryCount'],
  ] as const)('is a problem at its own field (%j)', (changes, field) => {
    expect([...problemsOf(leg(changes)).keys()]).toEqual([legKey('call', field)]);
  });

  it('is not turned into a number to judge it: a huge or odd value is left to the server', () => {
    expect(built(leg({ stopLossOn: true, stopLossValue: '999999999999999999999.123456789' })).legs[0]).toMatchObject({
      stop_loss: { value: '999999999999999999999.123456789' },
    });
  });

  it('says why re-entering on momentum cannot stand without a momentum rule', () => {
    const problems = problemsOf(leg({ reentryOn: true, reentryKind: 'on_momentum', momentumOn: false }));

    expect(problems.get(legKey('call', 'reentry'))).toMatch(/momentum/i);
  });

  it('allows re-entering on momentum once momentum is on', () => {
    expect(built(leg({ reentryOn: true, reentryKind: 'on_momentum', momentumOn: true, momentumValue: '5' })).legs[0]).toMatchObject({
      reentry_on_stop: { kind: 'on_momentum' },
    });
  });
});

describe('times', () => {
  it('a time outside the full session is a problem at its field and there is no body', () => {
    const problems = problemsOf({ ...newSetupDraft(SESSION_CLOCK), entryClock: formatClockText(OPEN - 1), exitClock: formatClockText(CLOSE + 1) });

    expect([...problems.keys()].sort()).toEqual(['entry', 'exit']);
  });

  it('the "no re-entry after" time is judged only while it is on', () => {
    const draft = { ...newSetupDraft(SESSION_CLOCK), noReentryClock: formatClockText(CLOSE + 1) };

    expect(problemsOf({ ...draft, noReentryOn: true }).has('noReentry')).toBe(true);
    expect(buildSetup({ ...draft, noReentryOn: false }, SESSION_CLOCK).ok).toBe(true);
  });

  it('a time after noon is stored from the close and one at noon from the open', () => {
    const draft = { ...newSetupDraft(SESSION_CLOCK), noReentryOn: true };

    expect(built({ ...draft, noReentryClock: '12:00' }).no_reentry_after).toEqual(minutes(12 * 60 - OPEN, 'open'));
    expect(built({ ...draft, noReentryClock: '12:01' }).no_reentry_after).toEqual(minutes(CLOSE - (12 * 60 + 1), 'close'));
  });
});

describe('costs and the other typed numbers', () => {
  it('leave as the text typed, trimmed, and never as numbers', () => {
    const body = built({ ...newSetupDraft(SESSION_CLOCK), slippage: ' 0.25 ', cash: '25000.50' });

    expect(body.slippage_percent).toBe('0.25');
    expect(body.starting_cash_usd).toBe('25000.50');
    expect(typeof body.slippage_percent).toBe('string');
    expect(typeof body.starting_cash_usd).toBe('string');
  });

  it.each(['', 'abc', '1e3', '-1', '1,5', '.5'])('does not read "%s" as a decimal', (text) => {
    const problems = problemsOf({ ...newSetupDraft(SESSION_CLOCK), slippage: text, cash: text });

    expect([...problems.keys()].sort()).toEqual(['cash', 'slippage']);
  });

  it('sends the per-contract fee only for the per-contract policy', () => {
    const draft = { ...newSetupDraft(SESSION_CLOCK), perContractFee: '0.65' };

    expect(built({ ...draft, feeKind: 'per_contract' }).fees).toEqual({ kind: 'per_contract', per_contract_usd: '0.65' });
    expect(built({ ...draft, feeKind: 'estimated' }).fees).toEqual({ kind: 'estimated' });
    expect(built({ ...draft, feeKind: 'not_charged' }).fees).toEqual({ kind: 'not_charged' });
  });

  it('reads the per-contract fee only for the per-contract policy', () => {
    const draft = { ...newSetupDraft(SESSION_CLOCK), perContractFee: 'free' };

    expect(buildSetup({ ...draft, feeKind: 'estimated' }, SESSION_CLOCK).ok).toBe(true);
    expect([...problemsOf({ ...draft, feeKind: 'per_contract' }).keys()]).toEqual(['perContractFee']);
  });

  it('asks for an underlying when none is picked', () => {
    expect([...problemsOf({ ...newSetupDraft(SESSION_CLOCK), underlying: '' }).keys()]).toEqual(['underlying']);
  });
});

describe('opening a served setup and sending it back', () => {
  it.each(servedExamples())('gives back the example it came from: $name', (served) => {
    const draft = draftFromSetup(served.setup ?? exampleBody(), SESSION_CLOCK);
    const body = built(draft);

    expect(body).toEqual(exampleBody(served.setup?.no_reentry_after ?? undefined));
  });

  it('keeps the ids of the legs it was given', () => {
    const draft = draftFromSetup(exampleBody(), SESSION_CLOCK);

    expect(draft.legs.map((leg) => leg.id)).toEqual(['call', 'put']);
  });

  it('reads numbers an imported file holds for decimals as the digits they are written with', () => {
    const imported: SetupRequest = { ...exampleBody(), slippage_percent: 0.25, starting_cash_usd: 5000, fees: { kind: 'per_contract', per_contract_usd: 0.65 } };

    const body = built(draftFromSetup(imported, SESSION_CLOCK));

    expect(body).toMatchObject({ slippage_percent: '0.25', starting_cash_usd: '5000', fees: { kind: 'per_contract', per_contract_usd: '0.65' } });
  });

  it('fills what an imported file leaves out with the server defaults', () => {
    const { slippage_percent: _slippage, price_step_usd: _step, version: _version, ...bare } = exampleBody();

    expect(built(draftFromSetup(bare, SESSION_CLOCK))).toMatchObject({ version: 1, slippage_percent: '0.3', price_step_usd: '0.01' });
  });

  it('keeps a time anchored the way the file had it while it is untouched, and applies the page rule once it is typed over', () => {
    const afterNoonFromOpen = { ...exampleBody(), no_reentry_after: minutes(5 * 60, 'open') };
    const draft = draftFromSetup(afterNoonFromOpen, SESSION_CLOCK);

    expect(built(draft).no_reentry_after).toEqual(minutes(5 * 60, 'open'));
    const retyped = { ...draft, noReentryClock: formatClockText(OPEN + 5 * 60 + 1) };
    expect(built(retyped).no_reentry_after).toEqual(minutes(CLOSE - (OPEN + 5 * 60 + 1), 'close'));
  });

  it('loads a time that names no clock time as an empty field with its problem', () => {
    const draft = draftFromSetup({ ...exampleBody(), entry: minutes(2000, 'open') }, SESSION_CLOCK);

    expect(draft.entryClock).toBe('');
    expect(problemsOf(draft).has('entry')).toBe(true);
  });

  it('reads the times with the clock it is given and not another', () => {
    const odd = { open_minute_et: 600, close_minute_et: 900 };

    expect(draftFromSetup(exampleBody(), odd)).toMatchObject({ entryClock: '10:05', exitClock: '14:14' });
  });
});
