import type { SavedSetup, ServedLeg, ServedSetup, SessionClock, SessionTimeWire, SetupLimits, SetupRequest, SetupSummary } from '../options-backtest.types';

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

/** What the server holds a setup to, as `GET /limits` serves it. */
export const SERVED_LIMITS: SetupLimits = {
  max_legs: 8,
  max_contracts_per_leg: 1000,
  max_reentries_per_leg: 20,
  max_strike_steps: 50,
  max_expiry_days: 730,
  max_range_sessions: 1300,
  setup_name: {
    pattern: '^[A-Za-z0-9](?:[A-Za-z0-9 _.,()-]{0,78}[A-Za-z0-9_.,()-])?$',
    sentence: 'A name is 1 to 80 characters: letters, digits, spaces and - _ . , ( ), starting with a letter or a digit and not ending with a space.',
  },
};

/**
 * The setup as the server reads a body back: every default filled in, a number written as the text it would be,
 * every unset rule an explicit null. It differs from the page's own body in exactly those ways, so a spec can tell
 * which of the two a call carries.
 */
export function readBackOf(body: SetupRequest): ServedSetup {
  return asServed({
    ...body,
    version: 1,
    slippage_percent: String(body.slippage_percent ?? '0.3'),
    price_step_usd: String(body.price_step_usd ?? '0.01'),
    starting_cash_usd: String(body.starting_cash_usd),
    fees: { kind: body.fees.kind, per_contract_usd: body.fees.per_contract_usd == null ? null : String(body.fees.per_contract_usd) },
    legs: body.legs.map((leg) => ({
      ...leg,
      stop_loss: leg.stop_loss == null ? null : { kind: leg.stop_loss.kind, value: String(leg.stop_loss.value) },
      momentum: leg.momentum == null ? null : { kind: leg.momentum.kind, value: String(leg.momentum.value) },
    })),
  });
}

export function summaryFor(text: string): SetupSummary {
  return { window: [`${text} window.`], legs: [], square_off: [`${text} square off.`], costs: [`${text} costs.`], notices: [], setup: exampleBody() };
}
