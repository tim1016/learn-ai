import type { components } from '../../../../src/app/api/broker.types';
import type { ChartDataResponse } from '../../../../src/app/markets/chart/data-lab-chart/data-lab-chart.component';
import type { DataLabPlanReceipt } from '../../../../src/app/markets/data/dataset-builder/data-lab-plan.service';
import type { IndicatorInfo } from '../../../../src/app/shared/indicator-catalog/indicator-catalog.service';
import { etMidnightMs } from '../../../../src/app/shared/date/et-midnight';
import { addDays, isWeekend, type IsoDate } from '../../../../src/app/shared/date/iso-date';
import type { ScopedHealth, ScopedHealthItem } from '../../../../src/app/shared/scoped-health/scoped-health';
import { WALK_NOW_MS, HELD_SYMBOLS, LAST_HELD_DAY } from './lake-world';
import type { Handler } from './mock-backend';
import { ok } from './mock-backend';

/**
 * What an instrument's place reads: its header facts, the chart, the lake's
 * report of the window, the Returns study and the dataset plan. Everything is
 * computed from the request the page sends, so the answer is only ever for the
 * window the page committed: a page that asked for another window would be
 * handed another count, and the walk reads the counts.
 *
 * The lake holds every weekday of the window except {@link MISSING_SESSION},
 * one finished session it lacks, so the Data tab has a gap to show before
 * anything is filled and the Returns study has one session fewer to measure.
 *
 * Nothing here answers a write. The reads that are POSTs (the chart, the
 * Returns study, the dataset plan) only compute from their bodies.
 */

type Schemas = components['schemas'];

/** The one finished session in every walk window that the lake does not hold. */
export const MISSING_SESSION: IsoDate = '2026-08-27';

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const sessionOpenMs = (day: IsoDate): number => etMidnightMs(day) + 9.5 * HOUR_MS;

/** The calendar day of a UTC-anchored instant, which is how the data plane's window edges are written. */
const utcDay = (ms: number): IsoDate => new Date(ms).toISOString().slice(0, 10);

/** Every weekday from `from` to `to`, both included: the walks' calendar has no holiday. */
export function sessionsBetween(from: IsoDate, to: IsoDate): IsoDate[] {
  const days: IsoDate[] = [];
  for (let day = from; day <= to; day = addDays(day, 1)) {
    if (!isWeekend(day)) days.push(day);
  }
  return days;
}

/** The sessions of the window the lake holds: all of them but the missing one. */
export const heldSessionsBetween = (from: IsoDate, to: IsoDate): IsoDate[] =>
  sessionsBetween(from, to).filter((day) => day !== MISSING_SESSION);

// ── Prices ───────────────────────────────────────────────────────────────

const LAST_CLOSE: Readonly<Record<string, number>> = { SPY: 659.28, QQQ: 566.4, AAPL: 242.1 };
const PRIOR_CLOSE: Readonly<Record<string, number>> = { SPY: 655.91, QQQ: 563.1, AAPL: 243.35 };

/** A close that drifts up and wobbles, the same for the same day whatever window asked for it. */
function closeOn(symbol: string, day: IsoDate): number {
  const base = LAST_CLOSE[symbol] ?? 100;
  const daysBack = (etMidnightMs(LAST_HELD_DAY) - etMidnightMs(day)) / DAY_MS;
  return Math.round((base * (1 - daysBack * 0.0004) + base * 0.02 * Math.sin(daysBack / 9)) * 100) / 100;
}

// ── The header and list facts ────────────────────────────────────────────

const SESSION_CLOSE_MS = (day: IsoDate): number => etMidnightMs(day) + 16 * HOUR_MS;

function factsFor(symbol: string): Schemas['InstrumentFacts'] {
  const price = LAST_CLOSE[symbol] ?? 100;
  const prior = PRIOR_CLOSE[symbol] ?? 100;
  return {
    symbol,
    quote: {
      status: 'available',
      price,
      basis: 'last_close',
      as_of_ms: SESSION_CLOSE_MS(LAST_HELD_DAY),
      move: {
        change: Math.round((price - prior) * 100) / 100,
        change_percent: Math.round(((price - prior) / prior) * 10_000) / 100,
        comparison_close: { price: prior, session_close_ms: SESSION_CLOSE_MS('2026-09-17') },
      },
      move_unavailable_reason: null,
      not_used: [{ basis: 'live_quote', reason: `No live line is streaming ${symbol} in this process (line state: idle).` }],
    },
    options_archive:
      symbol === 'AAPL'
        ? null
        : {
            kind: 'some_days',
            earliest_required_session_ms: sessionOpenMs('2025-09-22'),
            last_archived_session_ms: sessionOpenMs(LAST_HELD_DAY),
            complete_through_session_ms: null,
            missing_sessions: 3,
            capturing: true,
            reason: null,
          },
  };
}

// ── The lake's health, as the shell's chips and an instrument's notice read it ──

type DataKind = ScopedHealthItem['scope']['data_kind'];

function healthyItem(kind: DataKind, source: ScopedHealthItem['source'], affects: ScopedHealthItem['affects']): ScopedHealthItem {
  return {
    source,
    affects,
    scope: { data_kind: kind, instrument: null, breadth: 'broad', session_ms: null, mode: null },
    observed_ms: WALK_NOW_MS,
    freshness: 'fresh',
    severity: 'ok',
    needs_attention: false,
    summary: `${kind} is ok`,
    explanation: `${kind} is ok.`,
    link: null,
  };
}

export const HEALTHY: ScopedHealth = {
  generated_ms: WALK_NOW_MS,
  items: [
    healthyItem('stock_bars', 'lake_catalog', ['stock_history']),
    healthyItem('option_archive', 'option_archive', ['options_history']),
    healthyItem('lake_backup', 'backups', ['recovery']),
    healthyItem('ibkr_market_data', 'ibkr_feed', ['live_chain', 'bot_feed']),
  ],
};

// ── The chart ────────────────────────────────────────────────────────────

interface ChartRequest {
  readonly ticker: string;
  readonly from_date: IsoDate;
  readonly to_date: IsoDate;
}

function chartFor(request: ChartRequest): ChartDataResponse {
  const days = heldSessionsBetween(request.from_date, request.to_date);
  return {
    bars: days.map((day) => {
      const close = closeOn(request.ticker, day);
      return { t: sessionOpenMs(day), o: close * 0.998, h: close * 1.006, l: close * 0.993, c: close, v: 80_000_000 };
    }),
    indicators: [],
    quality: {
      raw_bar_count: days.length,
      resampled_bar_count: days.length,
      duplicates_removed: 0,
      gaps_found: 0,
      largest_gap_minutes: 0,
      missing_sessions: 0,
      session_coverage_pct: 100,
      synthetic_bars: 0,
      gap_details: [],
      missing_session_dates: [],
    },
    allowed_timeframes: ['1D', '1h', '30m', '15m'],
    estimated_bars_per_timeframe: { '1D': days.length },
    recommended_timeframe: '1D',
    meta: { cached_resample: false, cached_indicators: false },
    bar_sources: { boundary_ms_utc: null, notice_code: null },
    indicator_warmup: null,
  };
}

// ── What the lake holds for a window ─────────────────────────────────────

function instrumentDataFor(symbol: string, startMs: number, endMs: number, tree: string): Schemas['InstrumentDataResponse'] {
  const from = utcDay(startMs);
  const to = utcDay(endMs);
  const days = sessionsBetween(from, to);
  const calendarDays = Math.round((etMidnightMs(to) - etMidnightMs(from)) / DAY_MS) + 1;
  const missing = days.filter((day) => day === MISSING_SESSION);
  const gap = missing.length === 1 ? missing[0] : null;
  return {
    as_of_ms: WALK_NOW_MS,
    daily_rollup: { state: 'current' },
    data_root_id: '3f2a6c1e-8d4b-4f7a-9e21-5b0c7d9a1e42',
    data_type: 'trade',
    fill_window:
      gap === null ? null : { start_trading_date_ms: sessionOpenMs(gap), end_trading_date_ms: sessionOpenMs(gap) },
    market: 'usa',
    price_adjustment_mode: tree,
    provider_history_start_ms: sessionOpenMs('2021-09-21'),
    resolution: 'minute',
    sessions: days.map((day, index) =>
      day === gap
        ? { trading_date_ms: sessionOpenMs(day), state: 'missing', fetchable: true, reason: null }
        : { trading_date_ms: sessionOpenMs(day), state: 'ready', artifact_id: 1000 + index },
    ),
    summary: {
      before_provider_history: 0,
      building: 0,
      calendar_day_count: calendarDays,
      failed: 0,
      fetchable: gap === null ? 0 : 1,
      forming: 0,
      missing: gap === null ? 0 : 1,
      needs_rebuild: 0,
      provider_no_data: 0,
      ready: days.length - (gap === null ? 0 : 1),
      run_readable: gap === null,
      session_count: days.length,
      upcoming: 0,
    },
    symbol,
  };
}

// ── The Returns study ────────────────────────────────────────────────────

const RETURN_KINDS = ['close_to_close', 'session', 'overnight'] as const;
type ReturnKind = (typeof RETURN_KINDS)[number];

/** A day's move in percent: a fixed wobble, scaled so the three definitions differ. */
function moveOf(kind: ReturnKind, index: number): number {
  const wobble = 0.8 * Math.sin(index * 2.3) + 0.5 * Math.cos(index * 0.9) + 0.15 * Math.sin(index * 0.11);
  return Math.round(wobble * (kind === 'close_to_close' ? 1 : kind === 'session' ? 0.75 : 0.45) * 1000) / 1000;
}

/** The standard normal's distribution function, to six places (Abramowitz and Stegun 7.1.26). */
function normalCdf(x: number): number {
  const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
  const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
  const erf = 1 - poly * Math.exp(-(x * x) / 2);
  return x >= 0 ? 0.5 * (1 + erf) : 0.5 * (1 - erf);
}

function distributionFor(request: Schemas['ReturnDistributionRequest']): Schemas['ReturnDistributionResponse'] {
  const width = request.bin_width_pct ?? 0.5;
  const span = request.span_pct ?? 5;
  const requested = sessionsBetween(utcDay(request.from_ms_utc), utcDay(request.to_ms_utc));
  const held = requested.filter((day) => day !== MISSING_SESSION);
  const movesOfKind = (kind: ReturnKind): number[] => held.map((_, index) => moveOf(kind, index));
  const moves: Record<ReturnKind, number[]> = {
    close_to_close: movesOfKind('close_to_close'),
    session: movesOfKind('session'),
    overnight: movesOfKind('overnight'),
  };

  const interior = Math.round((2 * span) / width);
  const edges = Array.from({ length: interior + 1 }, (_, k) => Math.round((-span + k * width) * 1e6) / 1e6);
  const indexOf = (value: number): number => (value < edges[0] ? 0 : value >= edges[interior] ? interior + 1 : 1 + Math.floor((value - edges[0]) / width));

  const kinds = RETURN_KINDS.map((kind): Schemas['KindDistributionModel'] => {
    const values = moves[kind];
    const n = values.length;
    const mean = values.reduce((sum, v) => sum + v, 0) / n;
    const std = Math.sqrt(values.reduce((sum, v) => sum + (v - mean) ** 2, 0) / (n - 1));
    const skew = values.reduce((sum, v) => sum + ((v - mean) / std) ** 3, 0) / n;
    const kurt = values.reduce((sum, v) => sum + ((v - mean) / std) ** 4, 0) / n - 3;
    const sorted = [...values].sort((a, b) => a - b);
    const tail = sorted.slice(0, Math.max(1, Math.floor(n * 0.05)));
    const counts = Array.from({ length: interior + 2 }, () => 0);
    for (const value of values) counts[indexOf(value)] += 1;
    const best = values.indexOf(sorted[n - 1]);
    const worst = values.indexOf(sorted[0]);
    return {
      kind,
      bins: counts.map((count, index) => ({
        count,
        is_edge: index === 0 || index === interior + 1,
        lower_pct: index === 0 ? null : edges[index - 1],
        upper_pct: index === interior + 1 ? null : edges[index],
      })),
      normal_expected_counts: counts.map((_, index) => {
        const lower = index === 0 ? 0 : normalCdf((edges[index - 1] - mean) / std);
        const upper = index === interior + 1 ? 1 : normalCdf((edges[index] - mean) / std);
        return Math.round(n * (upper - lower) * 1000) / 1000;
      }),
      stats: {
        n_days: n,
        mean_pct: mean,
        std_pct: std,
        annualized_vol_pct: std * Math.sqrt(252),
        skewness: skew,
        excess_kurtosis: kurt,
        var_95_pct: sorted[Math.max(0, Math.floor(n * 0.05) - 1)],
        cvar_95_pct: tail.reduce((sum, v) => sum + v, 0) / tail.length,
        best_day: { session_open_ms_utc: sessionOpenMs(held[best]), value_pct: sorted[n - 1] },
        worst_day: { session_open_ms_utc: sessionOpenMs(held[worst]), value_pct: sorted[0] },
      },
    };
  });

  const missing = requested.length - held.length;
  return {
    meta: {
      adjustment: 'split_and_dividend',
      bin_width_pct: width,
      capture: { status: 'not_attempted', fetched_artifact_count: 0, detail: null },
      from_ms_utc: request.from_ms_utc,
      resolution: '1m',
      span_pct: span,
      symbol: request.symbol,
      to_ms_utc: request.to_ms_utc,
      warnings:
        missing === 0
          ? []
          : [`${missing} scheduled session(s) in the window are not captured in the lake; the study covers what is captured`],
    },
    coverage: {
      requested_sessions: requested.length,
      returned_sessions: held.length,
      missing_sessions: missing,
      excluded_sessions: 0,
      first_session_open_ms_utc: sessionOpenMs(held[0]),
      last_session_open_ms_utc: sessionOpenMs(held[held.length - 1]),
    },
    kinds,
    days: held.map((day, index) => ({
      session_open_ms_utc: sessionOpenMs(day),
      close_to_close_pct: moves.close_to_close[index],
      session_pct: moves.session[index],
      overnight_pct: moves.overnight[index],
      pre_market_pct: null,
      morning_pct: null,
      afternoon_pct: null,
      after_hours_pct: null,
      volume: 80_000_000,
      bin_indices: {
        close_to_close: indexOf(moves.close_to_close[index]),
        session: indexOf(moves.session[index]),
        overnight: indexOf(moves.overnight[index]),
      },
    })),
  };
}

// ── The dataset plan ─────────────────────────────────────────────────────

interface PlanRequest {
  readonly ticker: string;
  readonly start_ms_utc: number;
  readonly end_ms_utc: number;
}

function planFor(request: PlanRequest): DataLabPlanReceipt {
  const days = sessionsBetween(utcDay(request.start_ms_utc), utcDay(request.end_ms_utc));
  return {
    ticker: request.ticker,
    window_start_ms_utc: request.start_ms_utc,
    window_end_ms_utc: request.end_ms_utc,
    exchange_sessions: days,
    session_count: days.length,
    output_columns: ['time', 'open', 'high', 'low', 'close', 'volume', 'ema_20', 'rsi_14'],
    output_column_count: 8,
    time_column: 'time_america_chicago',
    estimated_bars: days.length,
    estimate_assumptions: ['One bar per scheduled session at the 1 day timeframe.'],
    estimate_provenance: 'Scheduled sessions from the canonical calendar; bars per session from the timeframe.',
    companion_dependencies: [],
    warnings: [],
    allowed_timeframes: ['1D', '1h'],
    recommended_timeframes: ['1D'],
    exchange: 'NYSE',
    calendar_timezone: 'America/New_York',
    calendar_version: 'walk-calendar',
  };
}

// ── The Options tab's Now · Snapshot chain ────────────────────────────────

/** The spot the snapshot chain is priced around: SPY's last close. */
const SPOT = LAST_CLOSE['SPY'];
const EXPIRY_DAYS: readonly IsoDate[] = ['2026-09-25', '2026-10-02', '2026-10-09', '2026-10-16', '2026-10-23', '2026-10-30', '2026-11-20', '2026-12-18'];
/** An expiry is its date's 16:00 ET anchor, as the data plane writes it. */
const expiryMs = (expiryDay: IsoDate): number => etMidnightMs(expiryDay) + 16 * HOUR_MS;

function contractsFor(expirationMs: number): Schemas['OptionsContractSnapshotItem'][] {
  const expiryDay = EXPIRY_DAYS.find((expiry) => expiryMs(expiry) === expirationMs) ?? EXPIRY_DAYS[0];
  const years = Math.max(1, (expirationMs - WALK_NOW_MS) / DAY_MS) / 365;
  const contracts: Schemas['OptionsContractSnapshotItem'][] = [];
  for (let strike = Math.round(SPOT) - 20; strike <= Math.round(SPOT) + 20; strike += 1) {
    for (const type of ['call', 'put'] as const) {
      const moneyness = (type === 'call' ? SPOT - strike : strike - SPOT);
      const timeValue = 7.5 * Math.sqrt(years * 12) * Math.exp(-((strike - SPOT) ** 2) / 260);
      const mid = Math.round((Math.max(moneyness, 0) + timeValue) * 100) / 100;
      const delta = normalCdf((SPOT - strike) / (SPOT * 0.16 * Math.sqrt(years)));
      contracts.push({
        ticker: `O:SPY${expiryDay.slice(2).replace(/-/g, '')}${type === 'call' ? 'C' : 'P'}${String(strike * 1000).padStart(8, '0')}`,
        contract_type: type,
        strike_price: strike,
        expiration_ms: expirationMs,
        break_even_price: type === 'call' ? strike + mid : strike - mid,
        implied_volatility: 0.16 + Math.abs(strike - SPOT) * 0.0011,
        open_interest: Math.round(12_000 * Math.exp(-((strike - SPOT) ** 2) / 300)),
        greeks: {
          delta: Math.round((type === 'call' ? delta : delta - 1) * 1000) / 1000,
          gamma: Math.round(0.05 * Math.exp(-((strike - SPOT) ** 2) / 120) * 10_000) / 10_000,
          theta: -Math.round(timeValue * 0.08 * 1000) / 1000,
          vega: Math.round(timeValue * 0.35 * 1000) / 1000,
        },
        last_quote: {
          bid: Math.max(0.01, Math.round((mid - 0.02) * 100) / 100),
          ask: Math.round((mid + 0.02) * 100) / 100,
          bid_size: 40,
          ask_size: 55,
          midpoint: mid,
          last_updated: SESSION_CLOSE_MS(LAST_HELD_DAY) * 1_000_000,
          timeframe: 'DELAYED',
        },
        last_trade: null,
        day: { open: mid, high: mid * 1.05, low: mid * 0.95, close: mid, volume: Math.round(900 * Math.exp(-((strike - SPOT) ** 2) / 200)), vwap: mid },
        standard: true,
      });
    }
  }
  return contracts;
}

// ── The indicator catalog the Chart's picker offers ──────────────────────

const INDICATORS: readonly IndicatorInfo[] = [
  {
    name: 'ema',
    category: 'Trend',
    description: 'Exponential moving average',
    configurable_params: [{ name: 'length', type: 'int', default: 20, min: 2, max: 400, description: 'Look-back in bars' }],
  },
  {
    name: 'rsi',
    category: 'Momentum',
    description: 'Relative strength index',
    configurable_params: [{ name: 'length', type: 'int', default: 14, min: 2, max: 200, description: 'Look-back in bars' }],
  },
];

const isHeld = (symbol: string): boolean => HELD_SYMBOLS.includes(symbol);

/** An instrument's place, answered for the window each request names. */
export const marketsHandlers: Handler = ({ method, path, query, body }) => {
  if (method === 'GET') {
    if (path === '/api/markets/instrument-facts') {
      const symbols = (query.get('symbols') ?? '').split(',').filter((symbol) => symbol !== '');
      const response: Schemas['InstrumentFactsResponse'] = {
        closes_adjustment: 'polygon_split_adjusted',
        facts: symbols.filter(isHeld).map(factsFor),
        generated_ms: WALK_NOW_MS,
      };
      return ok(response);
    }
    if (path === '/api/scoped-health') return ok(HEALTHY);
    if (path === '/api/dataset/available') {
      return ok({
        success: true,
        categories: { Trend: INDICATORS.filter((i) => i.category === 'Trend'), Momentum: INDICATORS.filter((i) => i.category === 'Momentum') },
        total: INDICATORS.length,
      });
    }
    if (path === '/api/data-lake/instrument-data') {
      const symbol = query.get('symbol') ?? '';
      if (!isHeld(symbol)) return null;
      return ok(
        instrumentDataFor(
          symbol,
          Number(query.get('start_trading_date_ms')),
          Number(query.get('end_trading_date_ms')),
          query.get('price_adjustment_mode') ?? 'polygon_split_adjusted',
        ),
      );
    }
    return null;
  }
  // The POSTs below only read: each computes its answer from its body.
  if (method === 'POST') {
    if (path === '/api/chart/data') return ok(chartFor(body as ChartRequest));
    if (path === '/api/research/return-distribution') return ok(distributionFor(body as Schemas['ReturnDistributionRequest']));
    if (path === '/api/dataset/plan') return ok(planFor(body as PlanRequest));
    if (path === '/api/options/expirations') {
      const response: Schemas['OptionsExpirationsResponse'] = { success: true, expirations: EXPIRY_DAYS.map(expiryMs), count: EXPIRY_DAYS.length };
      return ok(response);
    }
    if (path === '/api/snapshot/options-chain') {
      const request = body as Schemas['OptionsChainSnapshotRequest'];
      const contracts = contractsFor(request.expiration_ms ?? expiryMs(EXPIRY_DAYS[0]));
      const response: Schemas['OptionsChainSnapshotResponse'] = {
        success: true,
        underlying: { ticker: 'SPY', price: SPOT, change: 3.37, change_percent: 0.51 },
        contracts,
        count: contracts.length,
        risk_free_rate: 0.041,
        dividend_yield: 0.012,
        rate_source: 'walk',
        dividend_source: 'walk',
      };
      return ok(response);
    }
    if (path === '/api/snapshot/ticker') {
      const response: Schemas['StockSnapshotResponse'] = {
        success: true,
        snapshot: {
          ticker: 'SPY',
          day: { open: 656.1, high: 660.4, low: 654.9, close: SPOT, volume: 71_000_000, vwap: 658.2 },
          prev_day: { open: 654.8, high: 657.3, low: 653.2, close: PRIOR_CLOSE['SPY'], volume: 66_000_000, vwap: 655.4 },
          todays_change: 3.37,
          todays_change_percent: 0.51,
          updated: SESSION_CLOSE_MS(LAST_HELD_DAY) * 1_000_000,
        },
      };
      return ok(response);
    }
  }
  return null;
};
