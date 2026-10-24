import type { ResearchHistoryEntry, ResearchHistoryPage } from '../../../../src/app/research/history/research-history';
import { RECORD_ID, SAVED_AT_MS, storedRecord } from '../../../../src/app/research/stored-study/testing/stored-study-fixtures';
import type { Handler } from './mock-backend';
import { ok } from './mock-backend';

/**
 * Research as the owner finds it after a few weeks of use: saved runs in
 * several stores, one of them the feature-study run the walk reopens.
 *
 * The History endpoint filters and pages as the data plane does (`type`,
 * `symbol`, `page`, `page_size`), so a link that carries `?type=feature-study`
 * lists exactly the feature runs. Reading a saved run is a GET and starts
 * nothing: the world has no study endpoint that accepts a run, so a press of
 * Run study would be refused and fail the walk's ledger.
 */

const DAY_MS = 86_400_000;

/** The feature-study run the walk reopens, saved five days before the walk's "now". */
export const OLD_FEATURE_RUN_ID = RECORD_ID;
export const OLD_FEATURE_QUESTION = 'Does rsi_14 predict the next move of SPY?';
const OLD_FEATURE_SAVED_MS = SAVED_AT_MS - 5 * DAY_MS;

const OTHER_FEATURE_RUN_ID = 'c'.repeat(32);
const SIGNAL_RUN_ID = 'd'.repeat(32);

const WINDOW = { start_ms: 1_704_171_600_000, end_ms: 1_711_598_400_000 };

function entry(overrides: Partial<ResearchHistoryEntry> & Pick<ResearchHistoryEntry, 'type' | 'native_id' | 'question'>): ResearchHistoryEntry {
  return {
    completed_at_ms: null,
    created_at_ms: SAVED_AT_MS,
    headline: null,
    parent: null,
    provenance: null,
    status: 'completed',
    status_detail: null,
    symbols: ['SPY'],
    window: WINDOW,
    ...overrides,
  };
}

/** Newest first, as the endpoint orders them. */
const ENTRIES: readonly ResearchHistoryEntry[] = [
  entry({
    type: 'signal-study',
    native_id: SIGNAL_RUN_ID,
    question: 'Does a trading signal built on macd_signal hold up out of sample on SPY?',
    created_at_ms: SAVED_AT_MS - DAY_MS,
    completed_at_ms: SAVED_AT_MS - DAY_MS + 9_000,
    provenance: 'Polygon minute bars, extended session',
  }),
  entry({
    type: 'spec-run',
    native_id: 'spec-run-2026-09-19',
    question: 'spy_ema_crossover on SPY, 15m',
    created_at_ms: SAVED_AT_MS - 2 * DAY_MS,
    completed_at_ms: SAVED_AT_MS - 2 * DAY_MS + 40_000,
    headline: 'Return 4.2%',
  }),
  entry({
    type: 'feature-study',
    native_id: OTHER_FEATURE_RUN_ID,
    question: 'Does ema_ribbon_slope predict the next move of QQQ?',
    symbols: ['QQQ'],
    created_at_ms: SAVED_AT_MS - 3 * DAY_MS,
    completed_at_ms: SAVED_AT_MS - 3 * DAY_MS + 6_000,
    provenance: 'Polygon minute bars, extended session',
  }),
  entry({
    type: 'feature-study',
    native_id: OLD_FEATURE_RUN_ID,
    question: OLD_FEATURE_QUESTION,
    created_at_ms: OLD_FEATURE_SAVED_MS,
    completed_at_ms: OLD_FEATURE_SAVED_MS + 5_000,
    provenance: 'Polygon minute bars, extended session',
  }),
  entry({
    type: 'feature-experiment',
    native_id: 'experiment-17',
    question: 'rsi_14 on AAPL (earlier)',
    symbols: ['AAPL'],
    created_at_ms: SAVED_AT_MS - 90 * DAY_MS,
    completed_at_ms: SAVED_AT_MS - 90 * DAY_MS + 3_000,
  }),
];

function historyPage(query: URLSearchParams): ResearchHistoryPage {
  const type = query.get('type');
  const symbol = query.get('symbol');
  const matching = ENTRIES.filter((row) => (type === null || row.type === type) && (symbol === null || row.symbols.includes(symbol)));
  const pageSize = Number(query.get('page_size') ?? 25);
  const page = Number(query.get('page') ?? 1);
  return {
    entries: matching.slice((page - 1) * pageSize, page * pageSize),
    page,
    page_size: pageSize,
    partial: false,
    sources: [],
    symbols: [...new Set(ENTRIES.flatMap((row) => row.symbols))].sort(),
    total: matching.length,
  };
}

/** The saved run as the data plane stored it when the run ended. */
export const OLD_FEATURE_RECORD = storedRecord('feature-study', {
  created_at_ms: OLD_FEATURE_SAVED_MS,
  completed_at_ms: OLD_FEATURE_SAVED_MS + 5_000,
});

/** The History, and a saved feature-study run by its id; a feature study's own page reads the indicator catalog. */
export const researchHandlers: Handler = ({ method, path, query }) => {
  if (method !== 'GET') return null;
  if (path === '/api/research/history') return ok(historyPage(query));
  if (path === `/api/research/study-records/feature-study/${OLD_FEATURE_RUN_ID}`) return ok(OLD_FEATURE_RECORD);
  if (path === '/api/dataset/available') return ok({ success: true, categories: {}, total: 0 });
  return null;
};
