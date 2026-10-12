import { etMidnightMs } from '../../../../shared/date/et-midnight';
import { sessionOpenMs } from '../../../../shared/date-picker/testing/fake-trading-calendar';
import type { LegTrade, TradeContract, TradesPage } from '../run-detail.types';

/**
 * A small scene made by hand, the way the server serves one. Tuesday 2026-09-15 opens at 09:30 New York time; a minute
 * is written `at(m)`, the close of the bar m minutes after the open. The figures below were chosen so a reader can check
 * them (a call bought at 2.57 and sold at 1.82 is a loss of 0.75 a share, 75.00 a contract of 100, and 1.30 of fees makes
 * it 76.30), but the page never works one out from another: each is shown from its own field.
 */

export const TUESDAY = '2026-09-15';
export const WEDNESDAY = '2026-09-16';
export const MINUTE = 60_000;
export const TUESDAY_OPEN = sessionOpenMs(TUESDAY);
export const WEDNESDAY_OPEN = sessionOpenMs(WEDNESDAY);
/** The close of the bar `minute` minutes after Tuesday's open. */
export const at = (minute: number, open = TUESDAY_OPEN): number => open + minute * MINUTE;

/** The expiry anchor of a contract, 16:00 New York time on its date. */
const SIXTEEN_HUNDRED = 16 * 60 * MINUTE;
export const EXPIRY_MS = etMidnightMs('2026-10-16') + SIXTEEN_HUNDRED;

export const CALL_585: TradeContract = { symbol: 'SPY261016C00585000', underlying: 'SPY', right: 'call', strike_usd: '585.00', expiry_ms: EXPIRY_MS };
export const PUT_585: TradeContract = { symbol: 'SPY261016P00585000', underlying: 'SPY', right: 'put', strike_usd: '585.00', expiry_ms: EXPIRY_MS };
export const CALL_586: TradeContract = { symbol: 'SPY261016C00586000', underlying: 'SPY', right: 'call', strike_usd: '586.00', expiry_ms: EXPIRY_MS };

/** A trade that bought on momentum and was stopped out: every optional cell is filled. */
export function stoppedTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return {
    session_open_ms: TUESDAY_OPEN,
    trade_number: 0,
    leg_id: 'call',
    entry_number: 0,
    contract: CALL_585,
    contracts: 1,
    multiplier: 100,
    starting_price_usd: '2.00',
    momentum_level_usd: '2.50',
    stop_level_usd: '1.93',
    entry_decided_at_ms: at(7),
    entry: { bar_close_ms: at(8), seen_usd: '2.55', filled_usd: '2.57' },
    exit_reason: 'stop',
    exit_group: 0,
    exit_decided_at_ms: at(10),
    exit_kind: 'sale',
    sale: { bar_close_ms: at(11), seen_usd: '1.84', filled_usd: '1.82' },
    mark: null,
    entry_fee_usd: '0.65',
    exit_fee_usd: '0.65',
    pnl_usd: '-76.30',
    ...changes,
  };
}

/** The same leg's re-entry: a fresh contract, bought with no momentum wait, so it has a stop level and no starting price. */
export function reentryTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return {
    ...stoppedTrade(),
    trade_number: 1,
    entry_number: 1,
    contract: CALL_586,
    starting_price_usd: null,
    momentum_level_usd: null,
    stop_level_usd: '1.20',
    entry_decided_at_ms: at(11),
    entry: { bar_close_ms: at(12), seen_usd: '1.58', filled_usd: '1.60' },
    exit_reason: 'exit_time',
    exit_group: 1,
    exit_decided_at_ms: at(385),
    sale: { bar_close_ms: at(386), seen_usd: '1.90', filled_usd: '1.88' },
    pnl_usd: '26.70',
    ...changes,
  };
}

/** A leg that was marked at the end and not sold: the exit is a mark, never a sale. */
export function markedTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return {
    session_open_ms: TUESDAY_OPEN,
    trade_number: 2,
    leg_id: 'put',
    entry_number: 0,
    contract: PUT_585,
    contracts: 2,
    multiplier: 100,
    starting_price_usd: '1.20',
    momentum_level_usd: '1.50',
    stop_level_usd: null,
    entry_decided_at_ms: at(3),
    entry: { bar_close_ms: at(4), seen_usd: '1.50', filled_usd: '1.52' },
    exit_reason: 'marked_not_sold',
    exit_group: 2,
    exit_decided_at_ms: at(390),
    exit_kind: 'mark',
    sale: null,
    mark: { bar_close_ms: at(388), price_usd: '1.25' },
    entry_fee_usd: '1.30',
    exit_fee_usd: '0',
    pnl_usd: '-55.30',
    ...changes,
  };
}

/** A leg with no rules at all, bought at the entry time and closed at the exit time, on the next day. */
export function plainTrade(changes: Partial<LegTrade> = {}): LegTrade {
  return {
    ...stoppedTrade(),
    session_open_ms: WEDNESDAY_OPEN,
    trade_number: 0,
    leg_id: 'put',
    contract: PUT_585,
    starting_price_usd: null,
    momentum_level_usd: null,
    stop_level_usd: null,
    entry_decided_at_ms: at(5, WEDNESDAY_OPEN),
    entry: { bar_close_ms: at(6, WEDNESDAY_OPEN), seen_usd: '3.10', filled_usd: '3.12' },
    exit_reason: 'exit_time',
    exit_decided_at_ms: at(384, WEDNESDAY_OPEN),
    sale: { bar_close_ms: at(385, WEDNESDAY_OPEN), seen_usd: '3.40', filled_usd: '3.38' },
    entry_fee_usd: '0.65',
    exit_fee_usd: '0.65',
    pnl_usd: '24.70',
    ...changes,
  };
}

/** The four trades, in the order the server gives them: by session, then by the order the run recorded them. */
export const FOUR_TRADES: readonly LegTrade[] = [stoppedTrade(), reentryTrade(), markedTrade(), plainTrade()];

/** One page of trades as the server serves it. By default it is the only page: the four trades and no next page. */
export function tradesPage(changes: Partial<TradesPage> = {}): TradesPage {
  const trades = changes.trades ?? [...FOUR_TRADES];
  return {
    run_id: 7,
    total: trades.length,
    offset: 0,
    limit: 100,
    next_offset: null,
    trades,
    ...changes,
  };
}
