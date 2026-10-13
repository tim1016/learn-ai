import { sessionOpenMs } from '../../../shared/date-picker/testing/fake-trading-calendar';
import type {
  CapitalEvidence,
  CashLevel,
  DayRow,
  MonthRow,
  RunBanner,
  RunDetail,
  RunFigures,
  RunListItem,
  RunStamps,
  ServedSetup,
  SlippagePoint,
} from '../options-backtest.types';
import { exampleBody } from './fixtures';

/**
 * A run as the server serves it. The figures below were chosen by hand and are
 * mutually consistent where a reader would check them (7 of 12 trades won is
 * 0.583333; 2000.00 over 765.44 is 2.612876), but a page never works one out
 * from another: each is shown from its own field.
 */

export const CREATED_AT_MS = Date.UTC(2026, 8, 21, 15, 30, 0);

export function runItem(changes: Partial<RunListItem> = {}): RunListItem {
  return {
    id: 7,
    job_id: 'job-7',
    created_at_ms: CREATED_AT_MS,
    finished_at_ms: null,
    status: 'running',
    setup_name: null,
    underlying: 'SPY',
    first_session_open_ms: sessionOpenMs('2026-09-14'),
    last_session_open_ms: sessionOpenMs('2026-09-18'),
    sessions_requested: 5,
    fee_policy: 'Fees are not charged.',
    headline: null,
    failure: null,
    ...changes,
  };
}

export const DAY_ROWS: DayRow[] = [
  { session_open_ms: sessionOpenMs('2026-09-14'), status: 'traded', skip_reason: null, trades: 2, net_profit_usd: '150.25', fees_usd: '2.60', trades_won: 2, trades_lost: 0, trades_break_even: 0 },
  { session_open_ms: sessionOpenMs('2026-09-15'), status: 'traded', skip_reason: null, trades: 4, net_profit_usd: '-62.40', fees_usd: '5.20', trades_won: 1, trades_lost: 3, trades_break_even: 0 },
  { session_open_ms: sessionOpenMs('2026-09-16'), status: 'no_trade', skip_reason: null, trades: 0, net_profit_usd: '0.00', fees_usd: '0.00', trades_won: 0, trades_lost: 0, trades_break_even: 0 },
  { session_open_ms: sessionOpenMs('2026-09-17'), status: 'skipped', skip_reason: 'window_does_not_fit', trades: 0, net_profit_usd: '0.00', fees_usd: '0.00', trades_won: 0, trades_lost: 0, trades_break_even: 0 },
  { session_open_ms: sessionOpenMs('2026-09-18'), status: 'traded', skip_reason: null, trades: 6, net_profit_usd: '1146.71', fees_usd: '39.00', trades_won: 4, trades_lost: 1, trades_break_even: 1 },
];

export const MONTH_ROWS: MonthRow[] = [
  {
    year: 2026,
    month: 8,
    sessions: 3,
    sessions_traded: 2,
    trades: 5,
    net_profit_usd: '88.10',
    fees_usd: '6.50',
    trades_won: 3,
    trades_lost: 2,
    trades_break_even: 0,
    sessions_by_status: [
      { status: 'traded', count: 2 },
      { status: 'no_trade', count: 1 },
    ],
  },
  {
    year: 2026,
    month: 9,
    sessions: 7,
    sessions_traded: 6,
    trades: 7,
    net_profit_usd: '1146.46',
    fees_usd: '40.30',
    trades_won: 4,
    trades_lost: 2,
    trades_break_even: 1,
    sessions_by_status: [
      { status: 'traded', count: 6 },
      { status: 'skipped', count: 1 },
    ],
  },
];

/** The cash carried a run of ten sessions whole: a credit spread 5 wide that needed 380.00 of the cash it had. */
export function capitalEvidence(changes: Partial<CapitalEvidence> = {}): CapitalEvidence {
  return {
    sessions_tested: 10,
    sessions_funded_continuously: 10,
    ended_by: null,
    most_collateral_held_usd: '500.00',
    cash_needed_usd: '380.00',
    cash_needed_is_incomplete: false,
    least_free_cash_usd: '9620.00',
    largest_assignment_obligation_usd: '50000',
    closes_relied_on_own_relief: 0,
    ...changes,
  };
}

/** Half the setup's cash, the same, double and four times: the smallest cannot enter, and the others can. */
export const CASH_LADDER: CashLevel[] = [
  {
    multiple: '0.5',
    starting_cash_usd: '5000.0',
    is_setups_own: false,
    entries_filled: 0,
    entries_refused_for_cash: 24,
    net_profit_usd: '0',
    capital_evidence: capitalEvidence({ cash_needed_usd: '0', cash_needed_is_incomplete: true, least_free_cash_usd: '5000.0', most_collateral_held_usd: '0', largest_assignment_obligation_usd: '0' }),
  },
  {
    multiple: '1',
    starting_cash_usd: '10000',
    is_setups_own: true,
    entries_filled: 12,
    entries_refused_for_cash: 2,
    net_profit_usd: '1234.56',
    capital_evidence: capitalEvidence({ cash_needed_is_incomplete: true }),
  },
  {
    multiple: '2',
    starting_cash_usd: '20000',
    is_setups_own: false,
    entries_filled: 14,
    entries_refused_for_cash: 0,
    net_profit_usd: '1500.00',
    capital_evidence: capitalEvidence({ sessions_funded_continuously: 7, ended_by: { kind: 'position_marked', session_open_ms: sessionOpenMs('2026-09-17'), at_ms: null, free_cash_after_usd: null }, cash_needed_usd: '500.00' }),
  },
  {
    multiple: '4',
    starting_cash_usd: '40000',
    is_setups_own: false,
    entries_filled: 14,
    entries_refused_for_cash: 0,
    net_profit_usd: '1500.00',
    capital_evidence: capitalEvidence({ cash_needed_usd: '500.00' }),
  },
];

/** New York midnight of 2026-10-12, the day the requirement rule's page was read. */
export const RUN_STAMPS: RunStamps = {
  replay_rules_version: 1,
  requirement_rule: 'alpaca_covered_positions',
  requirement_rule_read_at_ms: 1_791_777_600_000,
};

export function figures(changes: Partial<RunFigures> = {}): RunFigures {
  return {
    sessions_tested: 10,
    sessions_traded: 8,
    sessions_no_trade: 1,
    sessions_skipped: 1,
    skipped_by_reason: [
      { reason: 'window_does_not_fit', count: 1 },
      { reason: 'underlying_did_not_trade', count: 0 },
    ],
    sessions_incomplete: 1,
    sessions_profitable: 5,
    sessions_losing: 2,
    sessions_break_even: 1,
    daily_win_rate: { numerator: '5', denominator: '8', rounded_to_6_places: '0.625000' },
    trades: 12,
    trades_won: 7,
    trades_lost: 4,
    trades_break_even: 1,
    trade_win_rate: { numerator: '7', denominator: '12', rounded_to_6_places: '0.583333' },
    exits_by_reason: [],
    starting_cash_usd: '10000.00',
    net_profit_usd: '1234.56',
    gross_profit_usd: '2000.00',
    gross_loss_usd: '765.44',
    profit_factor: { numerator_usd: '2000.00', denominator_usd: '765.44', rounded_to_6_places: '2.612876' },
    total_fees_usd: '46.80',
    slippage_paid_usd: '88.12',
    ending_cash_usd: '11234.56',
    drawdown_by_dollars: {
      fall_usd: '412.30',
      fall_ratio: { numerator_usd: '412.30', denominator_usd: '10900.00', rounded_to_6_places: '0.037826' },
      measured_at: 'session_end',
      peak_equity_usd: '10900.00',
      peak_session_open_ms: sessionOpenMs('2026-09-15'),
      trough_equity_usd: '10487.70',
      trough_session_open_ms: sessionOpenMs('2026-09-17'),
    },
    drawdown_by_fraction: {
      fall_usd: '400.00',
      fall_ratio: { numerator_usd: '400.00', denominator_usd: '10000.00', rounded_to_6_places: '0.040000' },
      measured_at: 'session_end',
      peak_equity_usd: '10000.00',
      peak_session_open_ms: null,
      trough_equity_usd: '9600.00',
      trough_session_open_ms: sessionOpenMs('2026-09-14'),
    },
    sessions_without_momentum: 3,
    entries_refused_for_cash: 2,
    re_entries_blocked_by_cutoff: 1,
    entries_dropped: 4,
    picks_refused_by_reason: [
      { reason: 'chain_incomplete', count: 1 },
      { reason: 'strike_not_listed', count: 2 },
    ],
    legs_without_a_starting_price: 5,
    re_entries_lost: 6,
    by_day: DAY_ROWS,
    by_month: MONTH_ROWS,
    capital_evidence: capitalEvidence({ cash_needed_is_incomplete: true }),
    cash_ladder: CASH_LADDER,
    stamps: RUN_STAMPS,
    ...changes,
  };
}

export const LADDER: SlippagePoint[] = [
  { slippage_percent: '0.0', is_setups_own: false, net_profit_usd: '1500.00', trades: 12, traded_sessions: 8, winning_sessions: 6 },
  { slippage_percent: '0.3', is_setups_own: true, net_profit_usd: '1234.56', trades: 12, traded_sessions: 8, winning_sessions: 5 },
  { slippage_percent: '0.6', is_setups_own: false, net_profit_usd: '-12.34', trades: 11, traded_sessions: 8, winning_sessions: 3 },
];

export function runBanner(changes: Partial<RunBanner> = {}): RunBanner {
  return {
    fills_are_simulated_from_trade_prices: true,
    bid_and_ask_are_available: false,
    slippage_percent: '0.3',
    price_step_usd: '0.01',
    fee_policy_kind: 'not_charged',
    fee_per_contract_usd: null,
    fee_policy: 'Fees are not charged.',
    ...changes,
  };
}

/** Why the server cannot open a run's setup, in its words (the setup was stored by an older version of the rules). */
export const SETUP_CANNOT_OPEN_REASON = 'This run’s setup can no longer be opened: the stored setup breaks a rule that came in after it was saved.';

/** A run whose setup the server cannot open: the figures are there, the banner and the setup are not, and the reason is. */
export function runWithoutSetup(changes: Partial<RunDetail> = {}): RunDetail {
  return runDetail({ setup: null, banner: null, setup_cannot_open_reason: SETUP_CANNOT_OPEN_REASON, ...changes });
}

export function runDetail(changes: Partial<RunDetail> = {}): RunDetail {
  const setup: ServedSetup = exampleBody();
  return {
    ...runItem({ status: 'complete', finished_at_ms: CREATED_AT_MS + 60_000 }),
    calendar_identity: 'NYSE',
    calendar_package_version: '5.1.0',
    setup,
    setup_cannot_open_reason: null,
    banner: runBanner(),
    figures: figures(),
    slippage: LADDER,
    slippage_tolerance: { highest_profitable_percent: '0.3', first_unprofitable_percent: '0.6', nothing_traded: false },
    ...changes,
  };
}
