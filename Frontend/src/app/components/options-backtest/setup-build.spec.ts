import { describe, expect, it } from 'vitest';

import { legFromBuilder, newLegBuilder } from './leg-draft';
import { buildSetup } from './setup-build';
import { draftFromSetup, newSetupDraft, type LegDraft, type SetupDraft } from './setup-draft';
import { formatClockText } from './session-time';
import { legKey } from './setup-problems';
import { EXAMPLE_NO_REENTRY, EXAMPLE_ONE_REENTRY, exampleBody, minutes, noReentryExampleBody, SESSION_CLOCK, servedExamples } from './testing/fixtures';
import type { ServedSetup, SetupRequest } from './options-backtest.types';

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

/** The other shipped example: the same legs with no re-entry rule, and no "no re-entry after" time. */
function noReentryExampleByHand(): SetupDraft {
  const { legs, ...rest } = exampleByHand();
  return { ...rest, noReentryOn: false, legs: legs.map((leg) => ({ ...leg, reentryOn: false })) };
}

describe('building the shipped examples by hand', () => {
  it('produces exactly the one-re-entry example JSON, and the no-re-entry example JSON with neither the re-entry rule nor the cutoff in it', () => {
    expect.soft(built(exampleByHand()), 'produces exactly the one-re-entry example JSON').toEqual(exampleBody());

    const body = built(noReentryExampleByHand());

    expect.soft(body, 'produces exactly the no-re-entry example JSON').toEqual(noReentryExampleBody());
    expect.soft(body, 'leaves the cutoff out of the no-re-entry example JSON').not.toHaveProperty('no_reentry_after');
    expect.soft(body.legs[0], 'leaves the re-entry rule out of the no-re-entry example JSON').not.toHaveProperty('reentry_on_stop');
  });
});

describe('a new setup', () => {
  it('starts on SPY with the times read from the clock and no legs, has no times before the clock is read, and builds the body of a new setup, with no legs yet, from its defaults', () => {
    const draft = newSetupDraft(SESSION_CLOCK);

    expect.soft(draft, 'starts on SPY with the times read from the clock and no legs').toMatchObject({
      underlying: 'SPY',
      entryClock: formatClockText(OPEN + 5),
      exitClock: formatClockText(CLOSE - 15),
      noReentryOn: false,
      squareOff: 'partial',
      slippage: '0.3',
      feeKind: 'not_charged',
      legs: [],
    });
    expect.soft(newSetupDraft(null), 'has no times before the clock is read').toMatchObject({ entryClock: '', exitClock: '', noReentryClock: '' });

    const body = built(newSetupDraft(SESSION_CLOCK));

    expect.soft(body, 'builds the body of a new setup, with no legs yet, from its defaults').toMatchObject({ version: 1, entry: minutes(5, 'open'), exit: minutes(15, 'close'), legs: [], price_step_usd: '0.01' });
    expect.soft('no_reentry_after' in body, 'leaves the "no re-entry after" time out of the body of a new setup').toBe(false);
  });
});

describe('a switched-off rule', () => {
  const leg = (changes: Partial<LegDraft>): SetupDraft => ({
    ...newSetupDraft(SESSION_CLOCK),
    legs: [{ ...legFromBuilder(newLegBuilder(), []), ...changes }],
  });

  it('sends nothing, whatever its fields hold, sends only the rule that is on, and turns the "no re-entry after" time off the same way', () => {
    const body = built(leg({ stopLossValue: 'not a number', momentumValue: '', reentryCount: 'many' }));

    expect.soft(body.legs[0], 'sends nothing, whatever its fields hold').toEqual({ leg_id: 'call', right: 'call', contracts: 1, expiry: { kind: 'this_week' }, strike: { kind: 'atm' } });

    const sent = built(leg({ momentumOn: true, momentumValue: '10' })).legs[0];

    expect.soft(sent, 'sends only the rule that is on: the momentum').toHaveProperty('momentum');
    expect.soft(sent, 'leaves the stop loss off when only the momentum is on').not.toHaveProperty('stop_loss');
    expect.soft(sent, 'leaves the re-entry off when only the momentum is on').not.toHaveProperty('reentry_on_stop');

    const on = built({ ...newSetupDraft(SESSION_CLOCK), noReentryOn: true });
    const off = built({ ...newSetupDraft(SESSION_CLOCK), noReentryOn: false, noReentryClock: 'garbage' });

    expect.soft(on.no_reentry_after, 'sends the "no re-entry after" time when it is on').toEqual(minutes(60, 'close'));
    expect.soft(off, 'turns the "no re-entry after" time off the same way').not.toHaveProperty('no_reentry_after');
  });

  it.each([
    ['sends the stop loss when it is on', { stopLossOn: true, stopLossKind: 'points', stopLossValue: ' 1.5 ' }, { stop_loss: { kind: 'points', value: '1.5' } }],
    ['sends the momentum when it is on', { momentumOn: true, momentumKind: 'points_down', momentumValue: '0.4' }, { momentum: { kind: 'points_down', value: '0.4' } }],
    ['sends the re-entry, with its count as a number, when it is on', { reentryOn: true, reentryKind: 'at_once', reentryCount: '3' }, { reentry_on_stop: { kind: 'at_once', count: 3 } }],
  ] as const)('%s', (_name, changes, sent) => {
    expect(built(leg(changes)).legs[0]).toMatchObject(sent);
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

  it('is not turned into a number to judge it, says why re-entering on momentum cannot stand without a momentum rule, and allows it once momentum is on', () => {
    expect.soft(built(leg({ stopLossOn: true, stopLossValue: '999999999999999999999.123456789' })).legs[0], 'is not turned into a number to judge it: a huge or odd value is left to the server').toMatchObject({
      stop_loss: { value: '999999999999999999999.123456789' },
    });

    const problems = problemsOf(leg({ reentryOn: true, reentryKind: 'on_momentum', momentumOn: false }));

    expect.soft(problems.get(legKey('call', 'reentry')), 'says why re-entering on momentum cannot stand without a momentum rule').toMatch(/momentum/i);
    expect.soft(built(leg({ reentryOn: true, reentryKind: 'on_momentum', momentumOn: true, momentumValue: '5' })).legs[0], 'allows re-entering on momentum once momentum is on').toMatchObject({
      reentry_on_stop: { kind: 'on_momentum' },
    });
  });
});

describe('a leg’s side', () => {
  const selling = (): SetupDraft => ({
    ...newSetupDraft(SESSION_CLOCK),
    legs: [
      legFromBuilder(newLegBuilder(), []),
      legFromBuilder({ ...newLegBuilder(), side: 'sell', strikeKind: 'atm_offset', strikeSteps: '2' }, ['call']),
    ],
  });

  it('is sent for a sold leg and none for a bought one, reads back from a served selling setup into the draft and builds the body it came from, and is bought for a leg the server served with none', () => {
    const [bought, sold] = built(selling()).legs;

    expect.soft(bought, 'a bought leg sends none, as the server writes none for it').not.toHaveProperty('side');
    expect.soft(sold, 'is sent for a sold leg').toMatchObject({ leg_id: 'call_2', side: 'sell', strike: { kind: 'atm_offset', steps: 2 } });

    const served: ServedSetup = {
      ...noReentryExampleBody(),
      legs: [
        { leg_id: 'long_call', right: 'call', contracts: 1, expiry: { kind: 'this_week' }, strike: { kind: 'atm' } },
        {
          leg_id: 'short_call',
          side: 'sell',
          right: 'call',
          contracts: 2,
          expiry: { kind: 'this_week' },
          strike: { kind: 'atm_offset', steps: 2 },
          stop_loss: { kind: 'percent', value: '150' },
        },
      ],
    };

    const draft = draftFromSetup(served, SESSION_CLOCK);
    const body = built(draft);

    expect.soft(draft.legs.map((leg) => [leg.id, leg.side]), 'reads back from a served selling setup into the draft: the sides').toEqual([
      ['long_call', 'buy'],
      ['short_call', 'sell'],
    ]);
    expect.soft(draft.legs[1], 'reads back from a served selling setup into the draft: the stop loss of the sold leg').toMatchObject({ stopLossOn: true, stopLossKind: 'percent', stopLossValue: '150' });
    expect.soft(body.legs, 'builds the body it came from').toEqual(served.legs);

    const unsided = draftFromSetup(exampleBody(), SESSION_CLOCK);

    expect.soft(unsided.legs.map((leg) => leg.side), 'is bought for a leg the server served with none, as it serves every bought leg').toEqual(['buy', 'buy']);
  });
});

describe('times', () => {
  it('a time outside the session, an entry at the open and an exit at the close, and a time beside a good one are problems at their own fields and there is no body; the "no re-entry after" time is judged only while it is on', () => {
    const outside = problemsOf({ ...newSetupDraft(SESSION_CLOCK), entryClock: formatClockText(OPEN - 1), exitClock: formatClockText(CLOSE + 1) });

    expect.soft([...outside.keys()].sort(), 'a time outside the full session is a problem at its field and there is no body').toEqual(['entry', 'exit']);

    const atTheEdges = problemsOf({ ...newSetupDraft(SESSION_CLOCK), entryClock: formatClockText(OPEN), exitClock: formatClockText(CLOSE) });

    expect.soft([...atTheEdges.keys()].sort(), 'an entry at the open and an exit at the close are problems at their own fields, and there is no body').toEqual(['entry', 'exit']);
    expect.soft(atTheEdges.get('entry'), 'an entry at the open says it must be at least a minute after the open').toMatch(/at least a minute after the open/);
    expect.soft(atTheEdges.get('exit'), 'an exit at the close says it must be at least a minute before the close').toMatch(/at least a minute before the close/);

    const beside = problemsOf({ ...newSetupDraft(SESSION_CLOCK), entryClock: formatClockText(OPEN - 1) });

    expect.soft([...beside.keys()], 'a time that is outside the session is not also judged against the other').toEqual(['entry']);

    const draft = { ...newSetupDraft(SESSION_CLOCK), noReentryClock: formatClockText(CLOSE + 1) };

    expect.soft(problemsOf({ ...draft, noReentryOn: true }).has('noReentry'), 'the "no re-entry after" time is judged while it is on').toBe(true);
    expect.soft(buildSetup({ ...draft, noReentryOn: false }, SESSION_CLOCK).ok, 'the "no re-entry after" time is not judged while it is off').toBe(true);
  });

  it('two times a loaded setup counts from the same end must be in order, and the exit says so', () => {
    const loaded = draftFromSetup({ ...exampleBody(), entry: minutes(30, 'open'), exit: minutes(10, 'open') }, SESSION_CLOCK);

    expect([...problemsOf(loaded).keys()]).toEqual(['exit']);
    expect(problemsOf(loaded).get('exit')).toMatch(/after the entry/);
    expect(built({ ...loaded, exitClock: formatClockText(CLOSE - 46) }).exit).toEqual(minutes(46, 'close'));
  });

});

describe('costs and the other typed numbers', () => {
  it('leave as the text typed, trimmed, and never as numbers; the per-contract fee is sent and read only for the per-contract policy; an underlying is asked for when none is picked', () => {
    const body = built({ ...newSetupDraft(SESSION_CLOCK), slippage: ' 0.25 ', cash: '25000.50' });

    expect.soft(body.slippage_percent, 'leaves the slippage as the text typed, trimmed').toBe('0.25');
    expect.soft(body.starting_cash_usd, 'leaves the starting cash as the text typed').toBe('25000.50');
    expect.soft(typeof body.slippage_percent, 'never turns the slippage into a number').toBe('string');
    expect.soft(typeof body.starting_cash_usd, 'never turns the starting cash into a number').toBe('string');

    const sentFee = { ...newSetupDraft(SESSION_CLOCK), perContractFee: '0.65' };

    expect.soft(built({ ...sentFee, feeKind: 'per_contract' }).fees, 'sends the per-contract fee for the per-contract policy').toEqual({ kind: 'per_contract', per_contract_usd: '0.65' });
    expect.soft(built({ ...sentFee, feeKind: 'estimated' }).fees, 'sends no per-contract fee for the estimated policy').toEqual({ kind: 'estimated' });
    expect.soft(built({ ...sentFee, feeKind: 'not_charged' }).fees, 'sends no per-contract fee for the not-charged policy').toEqual({ kind: 'not_charged' });

    const readFee = { ...newSetupDraft(SESSION_CLOCK), perContractFee: 'free' };

    expect.soft(buildSetup({ ...readFee, feeKind: 'estimated' }, SESSION_CLOCK).ok, 'does not read the per-contract fee for the estimated policy').toBe(true);
    expect.soft([...problemsOf({ ...readFee, feeKind: 'per_contract' }).keys()], 'reads the per-contract fee for the per-contract policy').toEqual(['perContractFee']);
    expect.soft([...problemsOf({ ...newSetupDraft(SESSION_CLOCK), underlying: '' }).keys()], 'asks for an underlying when none is picked').toEqual(['underlying']);
  });

  it.each(['', 'abc', '1e3', '-1', '1,5', '.5'])('does not read "%s" as a decimal', (text) => {
    const problems = problemsOf({ ...newSetupDraft(SESSION_CLOCK), slippage: text, cash: text });

    expect([...problems.keys()].sort()).toEqual(['cash', 'slippage']);
  });
});

describe('opening a served setup and sending it back', () => {
  it.each([
    { name: EXAMPLE_NO_REENTRY, sent: noReentryExampleBody() },
    { name: EXAMPLE_ONE_REENTRY, sent: exampleBody() },
  ])('gives back the example it came from, as the server lists it with its unset rules null: $name', ({ name, sent }) => {
    const served = servedExamples().find((example) => example.name === name)?.setup;
    if (served == null) throw new Error(`The server lists no example named ${name}.`);

    expect(built(draftFromSetup(served, SESSION_CLOCK))).toEqual(sent);
  });

  it('leaves a decimal the served setup lacks empty, with its problem, and fills in no default of its own, and reads every decimal as the text the server served it as', () => {
    const { slippage_percent: _slippage, price_step_usd: _step, ...bare } = exampleBody();

    const bareDraft = draftFromSetup(bare, SESSION_CLOCK);

    expect.soft(bareDraft, 'leaves a decimal the served setup lacks empty, and fills in no default of its own').toMatchObject({ slippage: '', priceStep: '' });
    expect.soft(problemsOf(bareDraft).has('slippage'), 'leaves a decimal the served setup lacks empty, with its problem').toBe(true);

    const served: ServedSetup = { ...exampleBody(), slippage_percent: '0.25', starting_cash_usd: '5000.50', fees: { kind: 'per_contract', per_contract_usd: '0.65' } };

    const body = built(draftFromSetup(served, SESSION_CLOCK));

    expect.soft(body, 'reads every decimal as the text the server served it as').toMatchObject({ slippage_percent: '0.25', starting_cash_usd: '5000.50', fees: { kind: 'per_contract', per_contract_usd: '0.65' } });
  });

  it('keeps a time anchored the way the file had it while it is untouched and applies the page rule once it is typed over, loads a time that names no clock time as an empty field with its problem, and reads the times with the clock it is given', () => {
    const afterNoonFromOpen = { ...exampleBody(), no_reentry_after: minutes(5 * 60, 'open') };
    const draft = draftFromSetup(afterNoonFromOpen, SESSION_CLOCK);

    expect.soft(built(draft).no_reentry_after, 'keeps a time anchored the way the file had it while it is untouched').toEqual(minutes(5 * 60, 'open'));
    const retyped = { ...draft, noReentryClock: formatClockText(OPEN + 5 * 60 + 1) };
    expect.soft(built(retyped).no_reentry_after, 'applies the page rule once the time is typed over').toEqual(minutes(CLOSE - (OPEN + 5 * 60 + 1), 'close'));

    const unnamed = draftFromSetup({ ...exampleBody(), entry: minutes(2000, 'open') }, SESSION_CLOCK);

    expect.soft(unnamed.entryClock, 'loads a time that names no clock time as an empty field').toBe('');
    expect.soft(problemsOf(unnamed).has('entry'), 'loads a time that names no clock time with its problem').toBe(true);

    const odd = { open_minute_et: 600, close_minute_et: 900 };

    expect.soft(draftFromSetup(exampleBody(), odd), 'reads the times with the clock it is given and not another').toMatchObject({ entryClock: '10:05', exitClock: '14:14' });
  });
});
