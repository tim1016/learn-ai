import type { ChainContract, ChainDay, ChainExpiry, ChainSide, DownloadAnswer, DownloadItem, ExpirySummary, ExpiryTag, TagSeries, UnderlyingPrice } from '../past-chain.types';

/**
 * Hand-made answers of the past-chain routes. Every instant below was worked out by hand and checked with an
 * independent script (`zoneinfo`), never taken from the code under test: New York is UTC-4 on these dates.
 */

export const UNDERLYING = 'SPY';
/** A Friday, an ordinary full session. */
export const TRADING_DATE = '2026-10-09';
/** New York midnight of it: 04:00 UTC. */
export const TRADING_DATE_MS = 1_791_518_400_000;
/** 09:30 ET and 16:00 ET of it. */
export const SESSION_OPEN_MS = 1_791_552_600_000;
export const SESSION_CLOSE_MS = 1_791_576_000_000;
/** 15:59 ET and 15:45 ET of it (19:59 and 19:45 UTC). */
export const MINUTE_1559_MS = 1_791_575_940_000;
export const MINUTE_1545_MS = 1_791_575_100_000;

/** The day after Thanksgiving, an early close at 13:00 ET: midnight 05:00 UTC, open 09:30, close 13:00. */
export const HALF_DAY = '2026-11-27';
export const HALF_DAY_MS = 1_795_755_600_000;
export const HALF_DAY_OPEN_MS = 1_795_789_800_000;
export const HALF_DAY_CLOSE_MS = 1_795_802_400_000;

/** An expiry is 16:00 New York time on its date: the anchor the server serves. */
export const EXPIRY_MS = {
  '2026-10-09': 1_791_576_000_000,
  '2026-10-12': 1_791_835_200_000,
  '2026-10-14': 1_792_008_000_000,
  '2026-10-16': 1_792_180_800_000,
  '2026-10-30': 1_793_390_400_000,
  '2026-12-18': 1_797_627_600_000,
} as const;

export function tag(series: TagSeries, changes: Partial<ExpiryTag> = {}): ExpiryTag {
  return { series, version: 1, evidence: 'the last session of its week', expires_today: false, days_to_expiry: 7, ...changes };
}

export function summary(expiryMs: number, expiryTag: ExpiryTag, changes: Partial<ExpirySummary> = {}): ExpirySummary {
  return { expiry_ms: expiryMs, tag: expiryTag, calls: 120, puts: 118, standard_calls: 118, standard_puts: 116, ...changes };
}

/** The day of the listing: three expiries by default (today's, next Friday, the month's third Friday). */
export function chainDay(changes: Partial<ChainDay> = {}): ChainDay {
  return {
    underlying: UNDERLYING,
    session_open_ms: SESSION_OPEN_MS,
    session_close_ms: SESSION_CLOSE_MS,
    listing_state: 'stored',
    listing_problem: null,
    expiries: [
      summary(EXPIRY_MS['2026-10-09'], tag('daily', { expires_today: true, days_to_expiry: 0, evidence: 'a session that is not the last of its week' })),
      summary(EXPIRY_MS['2026-10-16'], tag('monthly', { evidence: 'the third Friday of its month', days_to_expiry: 7 })),
      summary(EXPIRY_MS['2026-10-30'], tag('weekly', { days_to_expiry: 21 })),
    ],
    rows_left_out_unusable: 0,
    rows_left_out_expired: 0,
    ...changes,
  };
}

/** The strike in dollars as the server writes it: no exponent, no trailing zero ("602", "602.5"). */
export function dollarsOf(thousandths: number): string {
  const digits = String(thousandths);
  const whole = digits.slice(0, -3) || '0';
  const fraction = digits.slice(-3).padStart(3, '0').replace(/0+$/, '');
  return fraction === '' ? whole : `${whole}.${fraction}`;
}

/** An OCC symbol, written here only so that a spec can say which contracts it expects on the wire. */
export function occSymbol(expiryDate: string, right: 'call' | 'put', thousandths: number): string {
  return `${UNDERLYING}${expiryDate.slice(2, 4)}${expiryDate.slice(5, 7)}${expiryDate.slice(8, 10)}${right === 'call' ? 'C' : 'P'}${String(thousandths).padStart(8, '0')}`;
}

export interface ContractWanted {
  readonly right: 'call' | 'put';
  readonly thousandths: number;
  readonly state: ChainContract['price_state'];
  readonly price?: string;
  readonly timeMs?: number;
  readonly standard?: boolean;
  readonly problem?: string;
}

export function contract(wanted: ContractWanted): ChainContract {
  const traded = wanted.state === 'traded';
  return {
    symbol: occSymbol('2026-10-16', wanted.right, wanted.thousandths),
    strike: dollarsOf(wanted.thousandths),
    strike_thousandths: wanted.thousandths,
    standard: wanted.standard ?? true,
    steps_from_the_money: wanted.standard === false ? null : 0,
    price_state: wanted.state,
    price: traded ? (wanted.price ?? '1.25') : null,
    price_time_ms: traded ? (wanted.timeMs ?? MINUTE_1559_MS) : null,
    problem: wanted.state === 'unreadable' ? (wanted.problem ?? 'the bars of the contract cannot be read: the stored file is damaged (OptionBarsFileError)') : null,
  };
}

/** A side that lists exactly the contracts it shows; `changes` says otherwise (a window narrower than the listing, a different count). */
export function side(right: 'call' | 'put', contracts: readonly ChainContract[], moneyThousandths: number | null = 602_000, changes: Partial<ChainSide> = {}): ChainSide {
  return {
    right,
    listed: contracts.length,
    standard_listed: contracts.filter((each) => each.standard).length,
    at_the_money_strike: moneyThousandths === null ? null : dollarsOf(moneyThousandths),
    at_the_money_strike_thousandths: moneyThousandths,
    contracts: [...contracts],
    ...changes,
  };
}

/** What the server sends when the underlying has no price at the instant: no money strike, no contracts, and the counts it still knows. */
export function sideWithoutMoney(right: 'call' | 'put', listed: number, standardListed: number): ChainSide {
  return side(right, [], null, { listed, standard_listed: standardListed });
}

export function underlyingPrice(changes: Partial<UnderlyingPrice> = {}): UnderlyingPrice {
  return { state: 'priced', price: '602.40', price_time_ms: SESSION_CLOSE_MS, problem: null, ...changes };
}

/**
 * The chain of the Friday of 2026-10-16 at the close, three strikes a side around 602. Calls: 601 traded at 15:58...
 * Each state a contract can be in is there once: traded, did not trade, prices not stored, unreadable.
 */
export function chainExpiry(changes: Partial<ChainExpiry> = {}): ChainExpiry {
  return {
    underlying: UNDERLYING,
    session_open_ms: SESSION_OPEN_MS,
    session_close_ms: SESSION_CLOSE_MS,
    expiry_ms: EXPIRY_MS['2026-10-16'],
    at_ms: SESSION_CLOSE_MS,
    strikes_each_side: 10,
    listing_state: 'stored',
    listing_problem: null,
    expiry_listed: true,
    tag: tag('monthly', { evidence: 'the third Friday of its month' }),
    underlying_price: underlyingPrice(),
    calls: side('call', [
      contract({ right: 'call', thousandths: 601_000, state: 'traded', price: '3.25', timeMs: 1_791_575_880_000 }),
      contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '2.10', timeMs: MINUTE_1559_MS }),
      contract({ right: 'call', thousandths: 603_000, state: 'did_not_trade' }),
    ]),
    puts: side('put', [
      contract({ right: 'put', thousandths: 601_000, state: 'not_stored' }),
      contract({ right: 'put', thousandths: 602_000, state: 'unreadable', problem: 'the bars of SPY261016P00602000 for 2026-10-09 cannot be read: the stored file is damaged (OptionBarsFileError)' }),
      contract({ right: 'put', thousandths: 603_000, state: 'traded', price: '1.05', timeMs: MINUTE_1545_MS }),
    ]),
    ...changes,
  };
}

export function item(kind: DownloadItem['kind'], subject: string, outcome: DownloadItem['outcome'], reason: DownloadItem['reason'] = null, detail: string | null = null): DownloadItem {
  return { kind, subject, outcome, reason, detail };
}

export function downloadAnswer(items: readonly DownloadItem[], changes: Partial<DownloadAnswer> = {}): DownloadAnswer {
  return { underlying: UNDERLYING, session_open_ms: SESSION_OPEN_MS, session_close_ms: SESSION_CLOSE_MS, items: [...items], ...changes };
}
