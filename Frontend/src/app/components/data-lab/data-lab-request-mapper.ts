import {
  DataLabCompanionSettings,
  DataLabIndicatorInstance,
  DataLabWindowMsUtc,
} from './data-lab-workspace-store';

/* Pure request mappers (PRD §7.2 / §12): map DataLabWorkspaceStore state to
 * the existing wire payloads — the chart request body DataLabChartComponent
 * posts to `/api/chart/data` today, and the generate-zip payload shaped like
 * `_buildGenerateZipPayload` in data-lab.component.ts. Dates are derived from
 * the committed numeric UTC window as YYYY-MM-DD (UTC) strings, with the
 * numeric `start_ms_utc` / `end_ms_utc` fields included additively so the
 * backend can migrate to numeric authority. All functions are pure. */

/** Format an int64 ms UTC instant as a UTC calendar date (YYYY-MM-DD). Pure. */
export function utcMsToIsoDate(msUtc: number): string {
  const date = new Date(msUtc);
  const y = date.getUTCFullYear();
  const m = String(date.getUTCMonth() + 1).padStart(2, '0');
  const d = String(date.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** Final instant of the UTC day starting at `dayStartMsUtc` (23:59:59.999) —
 *  the window END anchor. The chart floors both endpoints to UTC dates
 *  (inclusive), while the news query and dataset requests read the same
 *  committed numbers as half-open instants; a midnight end makes a
 *  single-day window degenerate for those readers and drops the end date's
 *  data, so every date-intent → ms conversion here anchors the TO date at
 *  its day's end. Pure. */
export function utcDayEndMs(dayStartMsUtc: number): number {
  return dayStartMsUtc + 86_400_000 - 1;
}

export interface ChartRequestMapperInput {
  ticker: string;
  window: DataLabWindowMsUtc;
  timeframe: string;
  session: string;
  forwardFill: boolean;
  adjusted: boolean;
  indicators: readonly DataLabIndicatorInstance[];
  /** Mirrors the legacy compute-all switch; independent of the recipe. */
  computeAllIndicators: boolean;
}

export interface ChartIndicatorWireEntry {
  name: string;
  params: Record<string, number>;
}

/** Map the store's session vocabulary to the wire vocabulary. The store's
 *  default "Regular" is `regular`, but the Python chart/dataset endpoints
 *  only recognize `rth`; "extended" passes through unchanged. Pure. */
export function mapSessionToWire(session: string): string {
  return session === 'rth' || session === 'extended' ? session : 'rth';
}

export interface ChartRequestBody {
  ticker: string;
  from_date: string;
  to_date: string;
  start_ms_utc: number;
  end_ms_utc: number;
  timeframe: string;
  session: string;
  forward_fill: boolean;
  adjusted: boolean;
  indicators: ChartIndicatorWireEntry[];
  compute_all_indicators: boolean;
}

/** Map the committed workspace scope to the `/api/chart/data` request body.
 *  Same shape DataLabChartComponent posts today, plus additive numeric
 *  `start_ms_utc` / `end_ms_utc`. Pure. */
export function buildChartRequestBody(input: ChartRequestMapperInput): ChartRequestBody {
  return {
    ticker: input.ticker,
    from_date: utcMsToIsoDate(input.window.startMsUtc),
    to_date: utcMsToIsoDate(input.window.endMsUtc),
    start_ms_utc: input.window.startMsUtc,
    end_ms_utc: input.window.endMsUtc,
    timeframe: input.timeframe,
    session: mapSessionToWire(input.session),
    forward_fill: input.forwardFill,
    adjusted: input.adjusted,
    indicators: input.indicators.map(i => ({ name: i.canonicalKey, params: { ...i.params } })),
    compute_all_indicators: input.computeAllIndicators,
  };
}

export interface OptionsCompanionWireConfig {
  enabled: boolean;
  strikes_each_side: number;
  include_calls: boolean;
  include_puts: boolean;
  dte_distance: number;
  include_ohlcv: boolean;
  include_vwap: boolean;
  include_transactions: boolean;
  include_open_interest: boolean;
  include_iv: boolean;
  include_delta: boolean;
  include_gamma: boolean;
  include_theta: boolean;
  include_vega: boolean;
  include_rho: boolean;
  include_discontinuity: boolean;
  risk_free_rate: number;
  dividend_yield: number;
}

export interface GenerateZipMapperInput {
  ticker: string;
  window: DataLabWindowMsUtc;
  indicators: readonly DataLabIndicatorInstance[];
  session: string;
  forwardFill: boolean;
  adjusted: boolean;
  companions: DataLabCompanionSettings;
  options: OptionsCompanionWireConfig;
  /** Additional fixed flags carried from the legacy monolith payload. */
  warmup: boolean;
  /** Server-side dividend adjustment (distinct from split `adjusted`). */
  adjustForDividends: boolean;
  timespan: string;
  multiplier: number;
  sort: string;
  limit: number;
  /** IANA zone of dataset.csv's readable time column; `null` omits it. */
  timeZone: string | null;
  /** dataset.csv data columns; `null` exports every planned column. */
  columns: readonly string[] | null;
}

/** Map the committed workspace state to the generate-zip payload — the same
 *  shape `_buildGenerateZipPayload` produces today, plus additive numeric
 *  `start_ms_utc` / `end_ms_utc` and the dataset.csv column selection. Pure. */
export function buildGenerateZipPayload(input: GenerateZipMapperInput): Record<string, unknown> {
  return {
    ...buildDatasetPlanPayload(input),
    columns: input.columns === null ? null : [...input.columns],
  };
}

/** The `/api/dataset/plan` body for the same recipe: the generate payload
 *  without the column selection, because the plan lists the columns a
 *  selection is chosen from. Pure. */
export function buildDatasetPlanPayload(
  input: Omit<GenerateZipMapperInput, 'columns'>,
): Record<string, unknown> {
  const optionsConfig = input.companions.optionsCompanionEnabled
    ? { ...input.options, enabled: true }
    : null;
  return {
    ticker: input.ticker,
    from_date: utcMsToIsoDate(input.window.startMsUtc),
    to_date: utcMsToIsoDate(input.window.endMsUtc),
    start_ms_utc: input.window.startMsUtc,
    end_ms_utc: input.window.endMsUtc,
    indicator_entries: input.indicators.map(i => ({
      name: i.canonicalKey,
      params: { ...i.params },
    })),
    session: mapSessionToWire(input.session),
    forward_fill: input.forwardFill,
    fail_on_gaps: !input.forwardFill,
    adjusted: input.adjusted,
    adjust_for_dividends: input.adjustForDividends,
    warmup: input.warmup,
    timespan: input.timespan,
    multiplier: input.multiplier,
    sort: input.sort,
    limit: input.limit,
    options_companion: optionsConfig,
    include_quality_report: input.companions.includeQualityReport,
    include_previous_close: input.companions.includePreviousClose,
    include_splits: input.companions.includeSplits,
    include_dividends: input.companions.includeDividends,
    include_ticker_overview: input.companions.includeTickerOverview,
    include_news: input.companions.includeNews,
    include_financials: input.companions.includeFinancials,
    include_trades: input.companions.includeStockTrades,
    include_quotes: input.companions.includeStockQuotes,
    time_zone: input.timeZone,
  };
}
