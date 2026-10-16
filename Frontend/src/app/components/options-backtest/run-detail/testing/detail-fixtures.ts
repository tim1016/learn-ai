import { etMidnightMs } from '../../../../shared/date/et-midnight';
import { sessionOpenMs } from '../../../../shared/date-picker/testing/fake-trading-calendar';
import type { ComparedRun, LegTrade, RunComparison, SeriesReason, SessionDetail, SessionEvent, SessionFill, SessionLevel, SessionMark, SessionSeries, TradeContract, TradesPage } from '../run-detail.types';

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
    entry: { side: 'buy', bar_close_ms: at(8), seen_usd: '2.55', filled_usd: '2.57' },
    exit_reason: 'stop',
    exit_group: 0,
    exit_decided_at_ms: at(10),
    exit_kind: 'sale',
    sale: { side: 'sell', bar_close_ms: at(11), seen_usd: '1.84', filled_usd: '1.82' },
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
    entry: { side: 'buy', bar_close_ms: at(12), seen_usd: '1.58', filled_usd: '1.60' },
    exit_reason: 'exit_time',
    exit_group: 1,
    exit_decided_at_ms: at(385),
    sale: { side: 'sell', bar_close_ms: at(386), seen_usd: '1.90', filled_usd: '1.88' },
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
    entry: { side: 'buy', bar_close_ms: at(4), seen_usd: '1.50', filled_usd: '1.52' },
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
    entry: { side: 'buy', bar_close_ms: at(6, WEDNESDAY_OPEN), seen_usd: '3.10', filled_usd: '3.12' },
    exit_reason: 'exit_time',
    exit_decided_at_ms: at(384, WEDNESDAY_OPEN),
    sale: { side: 'sell', bar_close_ms: at(385, WEDNESDAY_OPEN), seen_usd: '3.40', filled_usd: '3.38' },
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
    total: trades.length,
    offset: 0,
    next_offset: null,
    trades,
    ...changes,
  };
}

// ── One day ───────────────────────────────────────────────────────────────

/** The server's words for each reason it withholds a series (`SERIES_WORDS`), so a spec shows them as they are. */
export const SERIES_WORDS: Readonly<Record<SeriesReason, string>> = {
  session_skipped: 'This session was skipped, so no bars were read for it.',
  no_pin: 'The run kept no record of reading these bars, so there is nothing to check them against.',
  gone: 'The stored bars are no longer in the lake.',
  changed: 'The stored bars have changed since this run.',
  not_confirmed: 'The stored bars cannot be confirmed against what this run read.',
  unreadable: 'The stored bars cannot be read now.',
  lake_unavailable: 'The lake could not be reached, so the bars cannot be checked against this run now.',
};

/** The minutes after the open at which a series has a point, and the close there. */
type Closes = readonly (readonly [minute: number, close: string])[];

export function shownSeries(symbol: string, contract: TradeContract | null, closes: Closes): SessionSeries {
  return {
    symbol,
    contract,
    state: 'shown',
    reason: null,
    message: null,
    points: closes.map(([minute, close]) => ({ at_ms: at(minute), close_usd: close })),
  };
}

export function withheldSeries(symbol: string, contract: TradeContract | null, reason: SeriesReason): SessionSeries {
  return { symbol, contract, state: 'not_shown', reason, message: SERIES_WORDS[reason], points: [] };
}

/** SPY across the toy session of twelve minutes: it did not print in the seventh. */
export const SPY_CLOSES: Closes = [
  [1, '585.10'], [2, '585.20'], [3, '585.15'], [4, '585.30'], [5, '585.25'], [6, '585.40'],
  [8, '585.55'], [9, '585.50'], [10, '585.45'], [11, '585.60'], [12, '585.65'],
];
/** The call did not trade in the fifth and sixth minutes: two runs of minutes, not one. */
export const CALL_CLOSES: Closes = [
  [2, '2.00'], [3, '2.10'], [4, '2.20'], [7, '2.50'], [8, '2.60'], [9, '2.30'], [10, '1.85'], [11, '1.80'], [12, '1.90'],
];
/** The put traded in four minutes running, then once, three minutes later. */
export const PUT_CLOSES: Closes = [[3, '1.40'], [4, '1.44'], [5, '1.45'], [6, '1.50'], [10, '1.25']];

/** The put as the toy session traded it: no rules, two contracts bought in the fourth minute and marked in the tenth. */
export function sceneMarkedPut(): LegTrade {
  return markedTrade({
    trade_number: 1,
    starting_price_usd: null,
    momentum_level_usd: null,
    entry_decided_at_ms: at(3),
    entry: { side: 'buy', bar_close_ms: at(4), seen_usd: '1.40', filled_usd: '1.42' },
    exit_decided_at_ms: at(11),
    mark: { bar_close_ms: at(10), price_usd: '1.25' },
    entry_fee_usd: '1.30',
    pnl_usd: '-35.30',
  });
}

export function event(minute: number, changes: Partial<SessionEvent> & Pick<SessionEvent, 'kind'>): SessionEvent {
  return { at_ms: at(minute), leg_id: 'call', entry_number: 0, contract: null, price_usd: null, reason: null, detail: null, ...changes };
}

export const SCENE_EVENTS: readonly SessionEvent[] = [
  event(1, { kind: 'picked', contract: CALL_585.symbol }),
  event(1, { kind: 'picked', leg_id: 'put', contract: PUT_585.symbol }),
  event(1, { kind: 'pick_refused', leg_id: 'wing', reason: 'strike_not_listed', detail: 'No 590 call was listed for 2026-10-09 with a trade that day.' }),
  event(2, { kind: 'starting_price_taken', contract: CALL_585.symbol, price_usd: '2.00' }),
  event(4, { kind: 'entry_filled', leg_id: 'put', contract: PUT_585.symbol, price_usd: '1.42' }),
  event(7, { kind: 'momentum_hit', contract: CALL_585.symbol, price_usd: '2.50' }),
  event(8, { kind: 'entry_filled', contract: CALL_585.symbol, price_usd: '2.57' }),
  event(10, { kind: 'stop_hit', contract: CALL_585.symbol, price_usd: '1.85' }),
  event(11, { kind: 'close_filled', contract: CALL_585.symbol, price_usd: '1.82' }),
  event(11, { kind: 're_entry_earned', entry_number: 1 }),
  event(11, { kind: 'exit_time', leg_id: 'put', contract: PUT_585.symbol, reason: 'exit_time' }),
  event(12, { kind: 'marked_not_sold', leg_id: 'put', contract: PUT_585.symbol, price_usd: '1.25' }),
];

export const SCENE_LEVELS: readonly SessionLevel[] = [
  { kind: 'starting_price', leg_id: 'call', entry_number: 0, contract: CALL_585.symbol, price_usd: '2.00', from_ms: at(2), to_ms: at(7) },
  { kind: 'momentum', leg_id: 'call', entry_number: 0, contract: CALL_585.symbol, price_usd: '2.50', from_ms: at(2), to_ms: at(7) },
  { kind: 'stop', leg_id: 'call', entry_number: 0, contract: CALL_585.symbol, price_usd: '1.93', from_ms: at(8), to_ms: at(10) },
];

export const SCENE_FILLS: readonly SessionFill[] = [
  { kind: 'entry', side: 'buy', leg_id: 'put', entry_number: 0, contract: PUT_585.symbol, contracts: 2, bar_close_ms: at(4), seen_usd: '1.40', filled_usd: '1.42', exit_reason: null },
  { kind: 'entry', side: 'buy', leg_id: 'call', entry_number: 0, contract: CALL_585.symbol, contracts: 1, bar_close_ms: at(8), seen_usd: '2.55', filled_usd: '2.57', exit_reason: null },
  { kind: 'exit', side: 'sell', leg_id: 'call', entry_number: 0, contract: CALL_585.symbol, contracts: 1, bar_close_ms: at(11), seen_usd: '1.84', filled_usd: '1.82', exit_reason: 'stop' },
];

export const SCENE_MARKS: readonly SessionMark[] = [
  { leg_id: 'put', entry_number: 0, contract: PUT_585.symbol, bar_close_ms: at(10), price_usd: '1.25', owed: false },
];

/**
 * The toy session of twelve minutes (Tuesday 2026-09-15, 09:30 to 09:42 New York time), a traded day: a call bought on
 * momentum in the eighth minute and stopped out in the eleventh, a put bought in the fourth and marked, not sold.
 * Profit: the call lost 76.30 (75.00 and 1.30 of fees) and the put 35.30 (34.00 and 1.30), so the day lost 111.60 and
 * cash went from 10000.00 to 9888.40. The page shows each of these from its own field.
 */
export function sessionDetail(changes: Partial<SessionDetail> = {}): SessionDetail {
  return {
    session_open_ms: TUESDAY_OPEN,
    session_close_ms: at(12),
    status: 'traded',
    skip_reason: null,
    cash_before_usd: '10000.00',
    cash_after_usd: '9888.40',
    profit_usd: '-111.60',
    trades: [stoppedTrade(), sceneMarkedPut()],
    events: [...SCENE_EVENTS],
    underlying: shownSeries('SPY', null, SPY_CLOSES),
    contracts: [shownSeries(CALL_585.symbol, CALL_585, CALL_CLOSES), shownSeries(PUT_585.symbol, PUT_585, PUT_CLOSES)],
    levels: [...SCENE_LEVELS],
    fills: [...SCENE_FILLS],
    marks: [...SCENE_MARKS],
    setup_cannot_open_reason: null,
    ...changes,
  };
}

/** A session that was skipped on its window: the stored facts, no trades, and no series, each withheld with a reason. */
export function skippedSession(changes: Partial<SessionDetail> = {}): SessionDetail {
  return sessionDetail({
    status: 'skipped',
    skip_reason: 'window_does_not_fit',
    cash_after_usd: '10000.00',
    profit_usd: '0.00',
    trades: [],
    events: [],
    underlying: withheldSeries('SPY', null, 'session_skipped'),
    contracts: [],
    levels: [],
    fills: [],
    marks: [],
    ...changes,
  });
}

// ── Two runs side by side ─────────────────────────────────────────────────

export function comparedRun(changes: Partial<ComparedRun> = {}): ComparedRun {
  return {
    id: 30,
    setup_name: 'Straddle',
    created_at_ms: Date.UTC(2026, 8, 21, 15, 30, 0),
    ...changes,
  };
}

/**
 * Two runs of SPY that differ in the re-entry of the call, in slippage and in fees, over the same dates. They share three
 * sessions. Run A's profits over those are 150.25, -62.40 and 32.65 (120.50 in all, two of them wins) and run B's are 10.00,
 * -80.25 and 0.00, where it skipped the day (-70.25 in all, one win). The page shows each of these figures from its own field and subtracts none.
 */
export function comparison(changes: Partial<RunComparison> = {}): RunComparison {
  return {
    underlying: 'SPY',
    a: comparedRun(),
    b: comparedRun({ id: 33, setup_name: null, created_at_ms: Date.UTC(2026, 8, 22, 15, 30, 0) }),
    setting_differences: [
      { label: 'leg call: re-entry on stop', a: 'not set', b: 'at once, up to 1 time' },
      { label: 'slippage', a: '0.3%', b: '0.45%' },
      { label: 'fees', a: 'Fees are not charged.', b: 'Fees were set by the owner at 0.65 a contract.' },
    ],
    range_difference: null,
    overlap: {
      shared_sessions: 3,
      only_in_a: 1,
      only_in_b: 0,
      a: { net_profit_usd: '120.50', trades: 6, winning_sessions: 2 },
      b: { net_profit_usd: '-70.25', trades: 7, winning_sessions: 1 },
      sessions: [
        { session_open_ms: sessionOpenMs('2026-09-14'), a_status: 'traded', b_status: 'traded', a_profit_usd: '150.25', b_profit_usd: '10.00' },
        { session_open_ms: sessionOpenMs('2026-09-15'), a_status: 'traded', b_status: 'traded', a_profit_usd: '-62.40', b_profit_usd: '-80.25' },
        { session_open_ms: sessionOpenMs('2026-09-16'), a_status: 'traded', b_status: 'skipped', a_profit_usd: '32.65', b_profit_usd: '0.00' },
      ],
    },
    ...changes,
  };
}
