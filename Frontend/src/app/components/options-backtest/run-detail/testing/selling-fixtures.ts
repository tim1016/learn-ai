import type { LegTrade, SessionDetail, SessionEvent, SessionFill, SessionLevel, SessionMark, TradeContract } from '../run-detail.types';
import { at, event, EXPIRY_MS, PUT_585, sessionDetail, shownSeries, TUESDAY_OPEN } from './detail-fixtures';

/**
 * A covered put spread as the server serves it (#3109): a put 580 bought to cover a put 585 sold, one contract each, in
 * the toy session of twelve minutes. The figures are worked in the comments and the page shows each from its own field.
 *
 * - `spreadSession()` closes: the 585 is sold to open at 2.18 and its stop level, 50 percent above that, is 3.27; a minute
 *   closes at 3.30, so it is bought back at 3.32 in the minute after. The 580 is bought at 1.42 and sold at 1.12.
 * - `owedSession()` ends marked: the 580 is not sold and the 585 is owed, at the last close of each.
 */

export const PUT_580: TradeContract = { symbol: 'SPY261016P00580000', underlying: 'SPY', right: 'put', strike_usd: '580.00', expiry_ms: EXPIRY_MS };

const BOUGHT_FILL = { side: 'buy' as const, seen_usd: '1.40', filled_usd: '1.42' };

/** The cover: a put 580 bought to open and sold to close at the exit time. */
export function coverTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return {
    session_open_ms: TUESDAY_OPEN,
    trade_number: 0,
    leg_id: 'long_put',
    entry_number: 0,
    contract: PUT_580,
    contracts: 1,
    multiplier: 100,
    starting_price_usd: null,
    momentum_level_usd: null,
    stop_level_usd: null,
    entry_decided_at_ms: at(3),
    entry: { ...BOUGHT_FILL, bar_close_ms: at(4) },
    exit_reason: 'exit_time',
    exit_group: 2,
    exit_decided_at_ms: at(11),
    exit_kind: 'sale',
    sale: { side: 'sell', bar_close_ms: at(12), seen_usd: '1.10', filled_usd: '1.12' },
    mark: null,
    entry_fee_usd: '0',
    exit_fee_usd: '0',
    pnl_usd: '-30.00',
    ...changes,
  };
}

/** The sold put 585: sold to open, stopped, and bought back. Its stop level is above what it was sold for. */
export function soldTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return {
    session_open_ms: TUESDAY_OPEN,
    trade_number: 1,
    leg_id: 'short_put',
    entry_number: 0,
    contract: PUT_585,
    contracts: 1,
    multiplier: 100,
    starting_price_usd: null,
    momentum_level_usd: null,
    stop_level_usd: '3.27',
    entry_decided_at_ms: at(3),
    entry: { side: 'sell', bar_close_ms: at(4), seen_usd: '2.20', filled_usd: '2.18' },
    exit_reason: 'stop',
    exit_group: 1,
    exit_decided_at_ms: at(9),
    exit_kind: 'buy_back',
    sale: { side: 'buy', bar_close_ms: at(10), seen_usd: '3.30', filled_usd: '3.32' },
    mark: null,
    entry_fee_usd: '0',
    exit_fee_usd: '0',
    pnl_usd: '-114.00',
    ...changes,
  };
}

/** The sold put 585 never bought back: marked at its last close, an amount it owes. */
export function owedTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return soldTrade({
    trade_number: 1,
    stop_level_usd: null,
    exit_reason: 'marked_not_bought_back',
    exit_decided_at_ms: at(11),
    exit_kind: 'mark',
    sale: null,
    mark: { bar_close_ms: at(10), price_usd: '2.40' },
    pnl_usd: '-22.00',
    ...changes,
  });
}

/** The cover marked and not sold, in the session whose sold leg is owed. */
export function heldCoverTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return coverTrade({
    exit_reason: 'marked_not_sold',
    exit_decided_at_ms: at(11),
    exit_kind: 'mark',
    sale: null,
    mark: { bar_close_ms: at(10), price_usd: '1.00' },
    pnl_usd: '-42.00',
    ...changes,
  });
}

const FILL = { contracts: 1, entry_number: 0 } as const;

export const SPREAD_FILLS: readonly SessionFill[] = [
  { kind: 'entry', side: 'buy', leg_id: 'long_put', contract: PUT_580.symbol, bar_close_ms: at(4), seen_usd: '1.40', filled_usd: '1.42', exit_reason: null, ...FILL },
  { kind: 'entry', side: 'sell', leg_id: 'short_put', contract: PUT_585.symbol, bar_close_ms: at(4), seen_usd: '2.20', filled_usd: '2.18', exit_reason: null, ...FILL },
  { kind: 'exit', side: 'buy', leg_id: 'short_put', contract: PUT_585.symbol, bar_close_ms: at(10), seen_usd: '3.30', filled_usd: '3.32', exit_reason: 'stop', ...FILL },
  { kind: 'exit', side: 'sell', leg_id: 'long_put', contract: PUT_580.symbol, bar_close_ms: at(12), seen_usd: '1.10', filled_usd: '1.12', exit_reason: 'exit_time', ...FILL },
];

/** The sold put's stop sits above the price it was sold for, and runs from the minute it filled to the minute the buy-back was decided. */
export const SPREAD_LEVELS: readonly SessionLevel[] = [
  { kind: 'stop', leg_id: 'short_put', entry_number: 0, contract: PUT_585.symbol, price_usd: '3.27', from_ms: at(4), to_ms: at(9) },
];

export const SPREAD_EVENTS: readonly SessionEvent[] = [
  event(4, { kind: 'entry_filled', leg_id: 'long_put', contract: PUT_580.symbol, price_usd: '1.42' }),
  event(4, { kind: 'entry_filled', leg_id: 'short_put', contract: PUT_585.symbol, price_usd: '2.18' }),
  event(9, { kind: 'stop_hit', leg_id: 'short_put', contract: PUT_585.symbol, price_usd: '3.30' }),
  event(10, { kind: 'close_filled', leg_id: 'short_put', contract: PUT_585.symbol, price_usd: '3.32' }),
  event(12, { kind: 'close_filled', leg_id: 'long_put', contract: PUT_580.symbol, price_usd: '1.12' }),
];

/** Each contract traded in every minute from the second to its last, so no minute of either is a lone one. */
const COVER_CLOSES = [[2, '1.40'], [3, '1.40'], [4, '1.41'], [5, '1.40'], [6, '1.35'], [7, '1.30'], [8, '1.20'], [9, '1.15'], [10, '1.10'], [11, '1.10'], [12, '1.12']] as const;
const SOLD_CLOSES = [[2, '2.15'], [3, '2.17'], [4, '2.20'], [5, '2.22'], [6, '2.30'], [7, '2.40'], [8, '2.50'], [9, '3.30'], [10, '3.32']] as const;

/** A day the spread opens at the fourth minute, the sold leg is stopped at the ninth and bought back, and the cover is sold at the end. */
export function spreadSession(changes: Partial<SessionDetail> = {}): SessionDetail {
  return sessionDetail({
    cash_before_usd: '10000.00',
    cash_after_usd: '9856.00',
    profit_usd: '-144.00',
    trades: [coverTrade(), soldTrade()],
    events: [...SPREAD_EVENTS],
    contracts: [shownSeries(PUT_580.symbol, PUT_580, [...COVER_CLOSES]), shownSeries(PUT_585.symbol, PUT_585, [...SOLD_CLOSES])],
    levels: [...SPREAD_LEVELS],
    fills: [...SPREAD_FILLS],
    marks: [],
    ...changes,
  });
}

export const OWED_MARKS: readonly SessionMark[] = [
  { leg_id: 'long_put', entry_number: 0, contract: PUT_580.symbol, bar_close_ms: at(10), price_usd: '1.00', owed: false },
  { leg_id: 'short_put', entry_number: 0, contract: PUT_585.symbol, bar_close_ms: at(10), price_usd: '2.40', owed: true },
];

/** A day that ends marked: the cover is not sold and the sold leg is owed. */
export function owedSession(changes: Partial<SessionDetail> = {}): SessionDetail {
  return sessionDetail({
    status: 'incomplete',
    cash_before_usd: '10000.00',
    cash_after_usd: '9936.00',
    profit_usd: '-64.00',
    trades: [heldCoverTrade({ trade_number: 0 }), owedTrade({ trade_number: 1 })],
    events: [
      event(4, { kind: 'entry_filled', leg_id: 'long_put', contract: PUT_580.symbol, price_usd: '1.42' }),
      event(4, { kind: 'entry_filled', leg_id: 'short_put', contract: PUT_585.symbol, price_usd: '2.18' }),
      event(12, { kind: 'marked_not_sold', leg_id: 'long_put', contract: PUT_580.symbol, price_usd: '1.00' }),
      event(12, { kind: 'marked_not_bought_back', leg_id: 'short_put', contract: PUT_585.symbol, price_usd: '2.40' }),
    ],
    contracts: [shownSeries(PUT_580.symbol, PUT_580, [[2, '1.40'], [3, '1.40'], [4, '1.41'], [10, '1.00']]), shownSeries(PUT_585.symbol, PUT_585, [[2, '2.15'], [4, '2.20'], [10, '2.40']])],
    levels: [],
    fills: SPREAD_FILLS.slice(0, 2).map((fill) => ({ ...fill })),
    marks: OWED_MARKS.map((mark) => ({ ...mark })),
    ...changes,
  });
}
