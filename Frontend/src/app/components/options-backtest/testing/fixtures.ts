import type { SavedSetup, ServedLeg, ServedSetup, SessionClock, SessionTimeWire, SetupSummary } from '../options-backtest.types';

export const MS_PER_MINUTE = 60_000;

/** A full session as `GET /session-clock` serves it: minutes past New York midnight. */
export const SESSION_CLOCK: SessionClock = { open_minute_et: 570, close_minute_et: 960 };

export function minutes(count: number, anchor: SessionTimeWire['anchor']): SessionTimeWire {
  return { anchor, offset_ms: count * MS_PER_MINUTE };
}

/** One of the shipped examples' legs, as the setup body holds it: stop loss and momentum on; the re-entry rule only when `reenters`. */
export function exampleLeg(legId: 'call' | 'put', reenters = true): ServedLeg {
  return {
    leg_id: legId,
    right: legId,
    contracts: 1,
    expiry: { kind: 'this_week' },
    strike: { kind: 'atm' },
    stop_loss: { kind: 'percent', value: '25' },
    momentum: { kind: 'percent_up', value: '25' },
    ...(reenters ? { reentry_on_stop: { kind: 'at_once', count: 1 } } : {}),
  };
}

/**
 * The shipped example "SPY premium momentum, one re-entry", in the form the routes accept
 * (`saved_setups.py`): each leg comes back once after a stop, until an hour before the close.
 */
export function exampleBody(): ServedSetup {
  return {
    version: 1,
    underlying: 'SPY',
    entry: minutes(5, 'open'),
    exit: minutes(46, 'close'),
    no_reentry_after: minutes(60, 'close'),
    square_off: 'complete',
    legs: [exampleLeg('call'), exampleLeg('put')],
    slippage_percent: '0.3',
    price_step_usd: '0.01',
    fees: { kind: 'not_charged' },
    starting_cash_usd: '10000',
  };
}

/**
 * The shipped example "SPY premium momentum, no re-entry": the same setup with no re-entry rule on its
 * legs and no "no re-entry after" time, so those keys are not there at all.
 */
export function noReentryExampleBody(): ServedSetup {
  const { no_reentry_after: _cutoff, ...rest } = exampleBody();
  return { ...rest, legs: [exampleLeg('call', false), exampleLeg('put', false)] };
}

/** A setup as `GET /setups` serves it: every optional key is there, null when unset. */
export function asServed(body: ServedSetup): NonNullable<SavedSetup['setup']> {
  return {
    ...body,
    no_reentry_after: body.no_reentry_after ?? null,
    legs: body.legs.map((leg) => ({
      ...leg,
      stop_loss: leg.stop_loss ?? null,
      momentum: leg.momentum ?? null,
      reentry_on_stop: leg.reentry_on_stop ?? null,
    })),
    fees: { kind: body.fees.kind, per_contract_usd: body.fees.per_contract_usd ?? null },
  };
}

function servedExample(name: string, body: ServedSetup): SavedSetup {
  return { name, example: true, saved_at_ms: null, cannot_open_reason: null, setup: asServed(body) };
}

export const EXAMPLE_NO_REENTRY = 'SPY premium momentum, no re-entry';
export const EXAMPLE_ONE_REENTRY = 'SPY premium momentum, one re-entry';

/** The two listed examples as the server lists them, in its order. */
export function servedExamples(): SavedSetup[] {
  return [servedExample(EXAMPLE_NO_REENTRY, noReentryExampleBody()), servedExample(EXAMPLE_ONE_REENTRY, exampleBody())];
}

export function servedSaved(name: string, setup: ServedSetup, savedAtMs = 1_760_000_000_000): SavedSetup {
  return {
    name,
    example: false,
    saved_at_ms: savedAtMs,
    cannot_open_reason: null,
    setup: asServed(setup),
  };
}

export function summaryFor(text: string): SetupSummary {
  return { window: `${text} window.`, legs: [], square_off: `${text} square off.`, costs: `${text} costs.`, notices: [] };
}
