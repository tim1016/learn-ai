import type { SavedSetup, ServedLeg, ServedSetup, SessionClock, SessionTimeWire, SetupSummary } from '../options-backtest.types';

export const MS_PER_MINUTE = 60_000;

/** A full session as `GET /session-clock` serves it: minutes past New York midnight. */
export const SESSION_CLOCK: SessionClock = { open_minute_et: 570, close_minute_et: 960 };

export function minutes(count: number, anchor: SessionTimeWire['anchor']): SessionTimeWire {
  return { anchor, offset_ms: count * MS_PER_MINUTE };
}

/** One of the shipped example's legs, as the setup body holds it: every rule on, nothing null. */
export function exampleLeg(legId: 'call' | 'put'): ServedLeg {
  return {
    leg_id: legId,
    right: legId,
    contracts: 1,
    expiry: { kind: 'this_week' },
    strike: { kind: 'atm' },
    stop_loss: { kind: 'percent', value: '25' },
    momentum: { kind: 'percent_up', value: '25' },
    reentry_on_stop: { kind: 'at_once', count: 1 },
  };
}

/**
 * The shipped example "SPY premium momentum, one re-entry", in the form the routes accept
 * (`saved_setups.py`). Pass another `no_reentry_after` for the other example.
 */
export function exampleBody(noReentryAfter: SessionTimeWire = minutes(60, 'close')): ServedSetup {
  return {
    version: 1,
    underlying: 'SPY',
    entry: minutes(5, 'open'),
    exit: minutes(46, 'close'),
    no_reentry_after: noReentryAfter,
    square_off: 'complete',
    legs: [exampleLeg('call'), exampleLeg('put')],
    slippage_percent: '0.3',
    price_step_usd: '0.01',
    fees: { kind: 'not_charged' },
    starting_cash_usd: '10000',
  };
}

/** A listed example as FastAPI serves it: every optional key is there, null when unset. */
export function servedExample(name: string, noReentryAfter: SessionTimeWire): SavedSetup {
  const body = exampleBody(noReentryAfter);
  return {
    name,
    example: true,
    saved_at_ms: null,
    cannot_open_reason: null,
    setup: { ...body, fees: { kind: 'not_charged', per_contract_usd: null } },
  };
}

export const EXAMPLE_NO_REENTRY = 'SPY premium momentum, no re-entry';
export const EXAMPLE_ONE_REENTRY = 'SPY premium momentum, one re-entry';

export function servedExamples(): SavedSetup[] {
  return [servedExample(EXAMPLE_NO_REENTRY, minutes(5, 'open')), servedExample(EXAMPLE_ONE_REENTRY, minutes(60, 'close'))];
}

export function servedSaved(name: string, setup: ServedSetup, savedAtMs = 1_760_000_000_000): SavedSetup {
  return {
    name,
    example: false,
    saved_at_ms: savedAtMs,
    cannot_open_reason: null,
    setup: { ...setup, fees: { kind: setup.fees.kind, per_contract_usd: setup.fees.per_contract_usd ?? null } },
  };
}

export function summaryFor(text: string): SetupSummary {
  return { window: `${text} window.`, legs: [], square_off: `${text} square off.`, costs: `${text} costs.`, notices: [] };
}
