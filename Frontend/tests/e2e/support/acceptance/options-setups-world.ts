import { OPTION_CATALOGUE } from '../../../../src/app/components/broker/broker-deploy-page/option-structure/option-structure.fixtures';
import type { GoldenOptionSetup } from '../../../../src/app/components/options-backtest/golden-option-setups.service';
import type {
  DayRow,
  RunDetail,
  RunFigures,
  RunListItem,
  SavedSetup,
  ServedSetup,
  SessionClock,
  SetupLimits,
  SetupSummary,
} from '../../../../src/app/components/options-backtest/options-backtest.types';
import type { EvidenceWorkspace } from '../../../../src/app/components/options-evidence/options-evidence.service';
import type { IndexedApproval, IndexedRun, OptionsSetupIndex } from '../../../../src/app/options-setups/index/options-setup-index.service';
import { etMidnightMs } from '../../../../src/app/shared/date/et-midnight';
import { LIVE_API, PAPER_API, NOW_MS } from '../owner-walk-world';
import type { Handler } from './mock-backend';
import { ok } from './mock-backend';

/**
 * The Options setups side of the acceptance walks: one saved setup, a tested
 * run of exactly that content, and the approved version that run made, ready
 * to deploy. Answered at the network edge, from the contract's own types.
 *
 * The run is complete, so Explore offers "Take to Golden"; the version is
 * ready, so the setup's "Deploy an options bot" lists it with a link. Nothing
 * here approves, revokes or starts anything: every read-shaped POST answers
 * and every command is refused.
 */

export const SETUP_NAME = 'SPY-morning-momentum';
/** The content the saved setup holds, as the server hashes it. */
export const SETUP_SHA256 = 'd1b43858a5665309c247592c4c93bbc51cbd3db2b75a8fbd5632d9aab1108b8a';
export const RUN_ID = 76;
export const VERSION_ID = 'ogs_dbe79f85399048f4a4decc3c86e1f3ff';

const BASE = '/api/research/options-backtest';
const SAVED_AT_MS = 1_790_000_000_000 - 3 * 86_400_000;
const RUN_CREATED_MS = NOW_MS - 2 * 86_400_000;
const APPROVED_MS = NOW_MS - 86_400_000;
const sessionOpen = (day: string): number => etMidnightMs(day) + 9.5 * 3_600_000;
const FIRST_SESSION = sessionOpen('2026-09-14');
const LAST_SESSION = sessionOpen('2026-09-18');

/** The setup as the library serves it: every unset rule is an explicit null. */
const SETUP: ServedSetup = {
  version: 1,
  underlying: 'SPY',
  entry: { anchor: 'open', offset_ms: 900_000 },
  exit: { anchor: 'close', offset_ms: 900_000 },
  no_reentry_after: null,
  square_off: 'partial',
  legs: [
    {
      leg_id: 'call_atm',
      right: 'call',
      contracts: 1,
      expiry: { kind: 'same_day' },
      strike: { kind: 'atm' },
      stop_loss: { kind: 'percent', value: '35' },
      momentum: { kind: 'percent_up', value: '35' },
      reentry_on_stop: null,
    },
    {
      leg_id: 'put_atm',
      right: 'put',
      contracts: 1,
      expiry: { kind: 'same_day' },
      strike: { kind: 'atm' },
      stop_loss: { kind: 'percent', value: '35' },
      momentum: { kind: 'percent_up', value: '50' },
      reentry_on_stop: null,
    },
  ],
  slippage_percent: '0.3',
  price_step_usd: '0.01',
  fees: { kind: 'per_contract', per_contract_usd: '0.65' },
  starting_cash_usd: '10000',
};

const SENTENCES = [
  'Leg call_atm: buys 1 at-the-money call expiring on the trading date itself.',
  'The buy waits until a minute closes at least 35% above where its premium stood at the entry time.',
  'Sells once a minute closes at least 35% below what was paid.',
  'Leg put_atm: buys 1 at-the-money put expiring on the trading date itself.',
  'The buy waits until a minute closes at least 50% above where its premium stood at the entry time.',
  'Sells once a minute closes at least 35% below what was paid.',
];

const SUMMARY: SetupSummary = {
  window: ['Enters 15 minutes after the open and exits 15 minutes before the close.'],
  legs: [
    { leg_id: 'call_atm', sentences: SENTENCES.slice(0, 3) },
    { leg_id: 'put_atm', sentences: SENTENCES.slice(3) },
  ],
  square_off: ['A leg still open at the exit is sold, and the others are left to expire.'],
  costs: ['Slippage is 0.3% of each price. Fees are 0.65 per contract.'],
  notices: [],
  setup: SETUP,
};

const SAVED: SavedSetup = {
  name: SETUP_NAME,
  example: false,
  saved_at_ms: SAVED_AT_MS,
  setup: SETUP,
  setup_sha256: SETUP_SHA256,
  cannot_open_reason: null,
};

const CLOCK: SessionClock = { open_minute_et: 570, close_minute_et: 960 };

const LIMITS: SetupLimits = {
  max_legs: 8,
  max_contracts_per_leg: 1000,
  max_reentries_per_leg: 20,
  max_strike_steps: 50,
  max_expiry_days: 730,
  max_range_sessions: 1300,
  max_strikes_each_side: 14,
  max_contracts_per_download: 60,
  setup_name: {
    pattern: '^[A-Za-z0-9](?:[A-Za-z0-9 _.,()-]{0,78}[A-Za-z0-9_.,()-])?$',
    sentence: 'A name is 1 to 80 characters: letters, digits, spaces and - _ . , ( ), starting with a letter or a digit and not ending with a space.',
  },
};

const RUN_ITEM: RunListItem = {
  id: RUN_ID,
  job_id: `job-${RUN_ID}`,
  created_at_ms: RUN_CREATED_MS,
  finished_at_ms: RUN_CREATED_MS + 60_000,
  status: 'complete',
  setup_name: SETUP_NAME,
  underlying: 'SPY',
  first_session_open_ms: FIRST_SESSION,
  last_session_open_ms: LAST_SESSION,
  sessions_requested: 5,
  fee_policy: 'Fees are 0.65 USD per contract.',
  headline: { net_profit_usd: '1234.56', ending_cash_usd: '11234.56', sessions_tested: 5, sessions_traded: 3, trades: 12, trades_won: 7 },
  failure: null,
};

const DAYS: DayRow[] = [
  { session_open_ms: sessionOpen('2026-09-14'), status: 'traded', skip_reason: null, trades: 2, net_profit_usd: '150.25', fees_usd: '2.60', trades_won: 2, trades_lost: 0, trades_break_even: 0 },
  { session_open_ms: sessionOpen('2026-09-15'), status: 'traded', skip_reason: null, trades: 4, net_profit_usd: '-62.40', fees_usd: '5.20', trades_won: 1, trades_lost: 3, trades_break_even: 0 },
  { session_open_ms: sessionOpen('2026-09-16'), status: 'no_trade', skip_reason: null, trades: 0, net_profit_usd: '0.00', fees_usd: '0.00', trades_won: 0, trades_lost: 0, trades_break_even: 0 },
  { session_open_ms: sessionOpen('2026-09-17'), status: 'skipped', skip_reason: 'window_does_not_fit', trades: 0, net_profit_usd: '0.00', fees_usd: '0.00', trades_won: 0, trades_lost: 0, trades_break_even: 0 },
  { session_open_ms: sessionOpen('2026-09-18'), status: 'traded', skip_reason: null, trades: 6, net_profit_usd: '1146.71', fees_usd: '39.00', trades_won: 4, trades_lost: 1, trades_break_even: 1 },
];

const FIGURES: RunFigures = {
  sessions_tested: 5,
  sessions_traded: 3,
  sessions_no_trade: 1,
  sessions_skipped: 1,
  skipped_by_reason: [{ reason: 'window_does_not_fit', count: 1 }],
  sessions_incomplete: 0,
  sessions_profitable: 2,
  sessions_losing: 1,
  sessions_break_even: 0,
  daily_win_rate: { numerator: '2', denominator: '3', rounded_to_6_places: '0.666667', display_percent_1_place: '66.7' },
  trades: 12,
  trades_won: 7,
  trades_lost: 4,
  trades_break_even: 1,
  trade_win_rate: { numerator: '7', denominator: '12', rounded_to_6_places: '0.583333', display_percent_1_place: '58.3' },
  exits_by_reason: [],
  starting_cash_usd: '10000.00',
  net_profit_usd: '1234.56',
  gross_profit_usd: '2000.00',
  gross_loss_usd: '765.44',
  profit_factor: { numerator_usd: '2000.00', denominator_usd: '765.44', rounded_to_6_places: '2.612876' },
  total_fees_usd: '46.80',
  slippage_paid_usd: '88.12',
  ending_cash_usd: '11234.56',
  equity_curve: [
    { session_open_ms: null, equity_usd: '10000.00' },
    ...DAYS.map((day, index) => ({ session_open_ms: day.session_open_ms, equity_usd: ['10150.25', '10087.85', '10087.85', '10087.85', '11234.56'][index] })),
  ],
  drawdown_by_dollars: {
    fall_usd: '62.40',
    display_percent_1_place: '0.6',
    fall_ratio: { numerator_usd: '62.40', denominator_usd: '10150.25', rounded_to_6_places: '0.006148' },
    peak_equity_usd: '10150.25',
    peak_session_open_ms: sessionOpen('2026-09-14'),
    trough_equity_usd: '10087.85',
    trough_session_open_ms: sessionOpen('2026-09-15'),
  },
  drawdown_by_fraction: {
    fall_usd: '62.40',
    display_percent_1_place: '0.6',
    fall_ratio: { numerator_usd: '62.40', denominator_usd: '10150.25', rounded_to_6_places: '0.006148' },
    peak_equity_usd: '10150.25',
    peak_session_open_ms: sessionOpen('2026-09-14'),
    trough_equity_usd: '10087.85',
    trough_session_open_ms: sessionOpen('2026-09-15'),
  },
  sessions_without_momentum: 1,
  entries_refused_for_cash: 0,
  re_entries_blocked_by_cutoff: 0,
  entries_dropped: 0,
  picks_refused_by_reason: [],
  legs_without_a_starting_price: 0,
  re_entries_lost: 0,
  entries_refused_by_reason: [],
  by_day: DAYS,
  by_month: [
    {
      year: 2026,
      month: 9,
      sessions: 5,
      sessions_traded: 3,
      trades: 12,
      net_profit_usd: '1234.56',
      fees_usd: '46.80',
      trades_won: 7,
      trades_lost: 4,
      trades_break_even: 1,
      sessions_by_status: [
        { status: 'traded', count: 3 },
        { status: 'no_trade', count: 1 },
        { status: 'skipped', count: 1 },
      ],
    },
  ],
  capital_evidence: {
    sessions_tested: 5,
    sessions_funded_continuously: 5,
    ended_by: null,
    most_collateral_held_usd: '0.00',
    cash_needed_usd: '380.00',
    cash_needed_is_incomplete: false,
    least_free_cash_usd: '9620.00',
    largest_assignment_obligation_usd: '0',
    closes_relied_on_own_relief: 0,
  },
  cash_ladder: [],
  stamps: { replay_rules_version: 1, requirement_rule: 'alpaca_covered_positions', requirement_rule_read_at_ms: RUN_CREATED_MS },
};

const RUN_DETAIL: RunDetail = {
  ...RUN_ITEM,
  calendar_identity: 'NYSE',
  calendar_package_version: '5.1.0',
  setup: SETUP,
  setup_cannot_open_reason: null,
  banner: {
    fills_are_simulated_from_trade_prices: true,
    bid_and_ask_are_available: false,
    slippage_percent: '0.3',
    price_step_usd: '0.01',
    fee_policy_kind: 'per_contract',
    fee_per_contract_usd: '0.65',
    fee_policy: 'Fees are 0.65 USD per contract.',
    statements: [],
  },
  figures: FIGURES,
  slippage: [
    { slippage_percent: '0.0', is_setups_own: false, net_profit_usd: '1500.00', trades: 12, traded_sessions: 3, winning_sessions: 3 },
    { slippage_percent: '0.3', is_setups_own: true, net_profit_usd: '1234.56', trades: 12, traded_sessions: 3, winning_sessions: 2 },
    { slippage_percent: '0.6', is_setups_own: false, net_profit_usd: '-12.34', trades: 11, traded_sessions: 3, winning_sessions: 1 },
  ],
  slippage_tolerance: { highest_profitable_percent: '0.3', first_unprofitable_percent: '0.6', nothing_traded: false },
};

/** The version this run made: owner-approved, exact content, and its proof still holds. */
const VERSION: GoldenOptionSetup = {
  id: VERSION_ID,
  name: SETUP_NAME,
  underlying: 'SPY',
  setup: SETUP,
  setup_sha256: SETUP_SHA256,
  source_run_id: RUN_ID,
  rules_digest: 'c'.repeat(64),
  proof_sha256: 'b'.repeat(64),
  status: 'ready',
  status_explanation: 'This exact tested setup is owner-approved and its repeatability proof matches the running rules.',
  created_at_ms: APPROVED_MS,
  approved_by: 'owner',
  note: 'Reviewed the exact run.',
  proof: {},
  events: [],
  evidence: {
    first_session_open_ms: FIRST_SESSION,
    last_session_open_ms: LAST_SESSION,
    sessions_tested: 5,
    trades: 12,
    net_profit_usd: '1234.56',
    starting_cash_usd: '10000',
    slippage_percent: '0.3',
    limitations: ['Trade bars do not validate quote spreads or actual live fills.'],
  },
};

const INDEXED_RUN: IndexedRun = {
  id: RUN_ID,
  recorded_name: SETUP_NAME,
  underlying: 'SPY',
  setup_sha256: SETUP_SHA256,
  unreadable_reason: null,
  created_at_ms: RUN_CREATED_MS,
  status: 'complete',
  first_session_open_ms: FIRST_SESSION,
  last_session_open_ms: LAST_SESSION,
  sessions_requested: 5,
  net_profit_usd: '1234.56',
};

const INDEXED_APPROVAL: IndexedApproval = {
  id: VERSION_ID,
  recorded_name: SETUP_NAME,
  underlying: 'SPY',
  setup_sha256: SETUP_SHA256,
  source_run_id: RUN_ID,
  study_id: null,
  origin: 'backtest',
  status: 'ready',
  created_at_ms: APPROVED_MS,
};

const INDEX: OptionsSetupIndex = {
  read_at_ms: NOW_MS,
  runs: [INDEXED_RUN],
  runs_total: 1,
  studies: [],
  studies_total: 0,
  approvals: [INDEXED_APPROVAL],
  approvals_total: 1,
};

/** What the retained-evidence read says of the tested run: the content it froze, and the version it made. */
const RUN_EVIDENCE: EvidenceWorkspace = {
  subject_kind: 'run',
  subject_id: String(RUN_ID),
  evidence_identity: `run:${RUN_ID}:${SETUP_SHA256}`,
  name: SETUP_NAME,
  setup: SETUP,
  setup_sha256: SETUP_SHA256,
  source_run_id: RUN_ID,
  study_id: null,
  period: { first_session_open_ms: FIRST_SESSION, last_session_open_ms: LAST_SESSION, scheduled_sessions: 5, observed_sessions: 5 },
  headline: 'Profitable in replay. Research qualification is missing.',
  replay: { status: 'profitable', explanation: '1234.56 USD after recorded modeled costs across 5 observed sessions. Descriptive replay evidence.' },
  coverage: { status: 'complete', explanation: '5 observed / 5 scheduled sessions. Session coverage does not certify minute valuation coverage.' },
  research: { status: 'not_recorded', explanation: 'No linked search qualification.' },
  final: { status: 'not_recorded', explanation: 'Final research evidence is not recorded for this replay.' },
  approval: { status: 'recorded', explanation: 'Approved from backtest.' },
  approvals: [
    {
      golden_id: VERSION_ID,
      source_run_id: RUN_ID,
      setup_sha256: SETUP_SHA256,
      source: 'backtest',
      identity_matches: true,
      proof_status: 'ready',
      proof_explanation: VERSION.status_explanation,
      note: 'Reviewed the exact run.',
      study_id: null,
      study_manifest_sha256: null,
      research_verdict: null,
      failed_rules: [],
      waived_gate_ids: [],
      exposure: null,
    },
  ],
  relations: [],
  settings_description: SENTENCES,
  frozen_search_settings: [],
};

/** What every setup page reads: the saved setups, the evidence recorded for them, and the server's clock, limits and read-back. */
export const optionsSetupsHandlers: Handler = ({ method, path, query }) => {
  if (method === 'GET' && path === `${BASE}/setups`) return ok([SAVED]);
  if (method === 'GET' && path === `${BASE}/setup-index`) return ok(INDEX);
  if (method === 'GET' && path === `${BASE}/session-clock`) return ok(CLOCK);
  if (method === 'GET' && path === `${BASE}/limits`) return ok(LIMITS);
  if (method === 'POST' && path === `${BASE}/setups/describe`) return ok(SUMMARY);
  if (method === 'GET' && path === `${BASE}/runs`) return ok([RUN_ITEM]);
  if (method === 'GET' && path === `${BASE}/runs/${RUN_ID}`) return ok(RUN_DETAIL);
  if (method === 'GET' && path === '/api/research/golden-option-setups') {
    const run = query.get('source_run_id');
    return ok(run === null || Number(run) === RUN_ID ? [VERSION] : []);
  }
  if (method === 'GET' && path === `/api/research/golden-option-setups/${VERSION_ID}`) return ok(VERSION);
  if (method === 'GET' && path === `/api/research/options-golden-search/workspace/run/${RUN_ID}`) return ok(RUN_EVIDENCE);
  return null;
};

/** What the options Deploy form reads beside the account's own deploy view: the structure catalogue. */
export const optionsDeployHandlers: Handler = ({ method, path }) => {
  if (method !== 'GET') return null;
  for (const scope of [PAPER_API, LIVE_API]) {
    if (path === `${scope}/bots/option-structures`) return ok(OPTION_CATALOGUE);
  }
  return null;
};
