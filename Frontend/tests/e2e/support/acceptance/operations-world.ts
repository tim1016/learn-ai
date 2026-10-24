import type { components } from '../../../../src/app/api/broker.types';
import type { CoverageDay, CoverageResponse, RunReadinessResponse } from '../../../../src/app/shared/data-lake/data-lake.types';
import { etMidnightMs } from '../../../../src/app/shared/date/et-midnight';
import type { GoldenFixturesCatalog } from '../../../../src/app/services/golden-fixtures.types';
import { NOW_MS } from '../owner-walk-world';
import { addDays, isWeekend } from '../../../../src/app/shared/date/iso-date';
import { FIRST_HELD_DAY, HELD_SYMBOLS, LAST_HELD_DAY } from './lake-world';
import type { Handler } from './mock-backend';
import { ok } from './mock-backend';

type ScopedHealthItem = components['schemas']['HealthItem'];
type ArchiveHealth = components['schemas']['OptionArchiveHealth'];

/**
 * Operations and the health behind it, answered at the network edge: the
 * scoped-health projection a page's notice and the top bar's chips read, and
 * the option archive's own status that the Option archive and Backups tabs show.
 *
 * The two are separate sources, as in the data plane: the walks can fail the
 * archive and leave the lake healthy (T20), and a page's notice is filtered by
 * the operations it names, so a stock-bar page hears nothing of the archive.
 */

/**
 * How the option archive stands in a walk. The lake and the IBKR feed are healthy in every one of them.
 * `unreadable`: the archive's status cannot be read, so the archive and the backups it reports are unknown
 * and the lake is untouched (T20).
 */
export type ArchiveCondition = 'healthy' | 'needs-attention' | 'unreadable';

/** The underlying whose archive is behind: its card is where the notice's link lands. */
export const ARCHIVE_UNDERLYING = 'SPY';
export const ARCHIVE_PENDING = 12;
export const ARCHIVE_PARTIAL = 1;
export const ARCHIVE_LOST = 0;
export const ARCHIVE_REASON = 'Archive gaps need attention before their vendor deadline.';

const OBSERVED_MS = NOW_MS - 120_000;
const DAY_MS = 86_400_000;

const item = (
  data_kind: ScopedHealthItem['scope']['data_kind'],
  source: ScopedHealthItem['source'],
  affects: ScopedHealthItem['affects'],
  severity: ScopedHealthItem['severity'],
  text: { readonly summary: string; readonly explanation: string },
  link: ScopedHealthItem['link'],
  instrument: string | null = null,
): ScopedHealthItem => ({
  source,
  affects,
  scope: { data_kind, instrument, breadth: instrument === null ? 'broad' : 'scoped', session_ms: null, mode: null },
  observed_ms: OBSERVED_MS,
  freshness: 'fresh',
  severity,
  needs_attention: severity === 'warning' || severity === 'critical' || severity === 'unknown',
  summary: text.summary,
  explanation: text.explanation,
  link,
});

function projection(archive: ArchiveCondition): ScopedHealthItem[] {
  const archiveSeverity: ScopedHealthItem['severity'] = archive === 'needs-attention' ? 'warning' : archive === 'unreadable' ? 'unknown' : 'ok';
  const archiveText = {
    healthy: { summary: 'SPY option archive is complete', explanation: 'The SPY option archive holds every session an options run needs.' },
    'needs-attention': {
      summary: `SPY option archive is behind by ${ARCHIVE_PENDING} sessions`,
      explanation: `The SPY option archive is missing ${ARCHIVE_PENDING} sessions before their vendor deadline, so a run on those dates cannot be reproduced yet.`,
    },
    unreadable: { summary: 'The option archive status could not be read', explanation: 'The option archive status could not be read, so whether it holds what a run needs is not known.' },
  }[archive];
  const backupSeverity: ScopedHealthItem['severity'] = archive === 'unreadable' ? 'unknown' : 'ok';
  const backupText = (what: string) =>
    archive === 'unreadable'
      ? { summary: `The ${what} status could not be read`, explanation: `The ${what} status could not be read, because the archive status that reports it was not answered.` }
      : { summary: `The ${what} is current`, explanation: `The ${what} is current.` };
  return [
    item('option_archive', 'option_archive', ['options_history'], archiveSeverity, archiveText, { tab: 'option-archive', query: {}, fragment: `option-archive-${ARCHIVE_UNDERLYING}` }, ARCHIVE_UNDERLYING),
    item('stock_bars', 'lake_catalog', ['stock_history'], 'ok', { summary: 'The lake holds its stock bars', explanation: 'Every symbol the lake holds is complete over its held span.' }, { tab: 'lake', query: {} }),
    item('lake_backup', 'backups', ['recovery'], backupSeverity, backupText('lake backup'), { tab: 'backups', query: {}, fragment: 'option-archive-backup' }),
    item('restore_check', 'backups', ['recovery'], backupSeverity, backupText('weekly restore check'), { tab: 'backups', query: {}, fragment: 'option-archive-restore' }),
    item('ibkr_market_data', 'ibkr_feed', ['live_chain', 'bot_feed'], 'ok', { summary: 'The IBKR feed is connected', explanation: 'The IBKR feed is connected.' }, null),
  ];
}

/** The worst finding first, as the data plane sends the projection. */
const WORST_FIRST: readonly ScopedHealthItem['severity'][] = ['critical', 'unknown', 'warning', 'info', 'ok'];

/** `GET /api/scoped-health`: what the data plane says, filtered as it filters (by the operations asked about and the one symbol). */
export function scopedHealthHandler(archive: ArchiveCondition): Handler {
  return ({ method, path, query }) => {
    if (method !== 'GET' || path !== '/api/scoped-health') return null;
    const operations = query.getAll('operation');
    const instrument = query.get('instrument');
    const items = projection(archive)
      .filter((found) => operations.length === 0 || found.affects.some((operation) => operations.includes(operation)))
      .filter((found) => instrument === null || found.scope.instrument === null || found.scope.instrument === instrument)
      .sort((a, b) => WORST_FIRST.indexOf(a.severity) - WORST_FIRST.indexOf(b.severity));
    return ok({ generated_ms: OBSERVED_MS, items });
  };
}

/** The archive's own status: per underlying, what is preserved and what is behind, and the backup and restore evidence. */
function archiveStatus(archive: ArchiveCondition): ArchiveHealth {
  const behind = archive === 'needs-attention';
  return {
    enabled: true,
    generated_ms: OBSERVED_MS,
    underlyings: [
      {
        underlying: ARCHIVE_UNDERLYING,
        severity: behind ? 'at_risk' : 'healthy',
        phase: behind ? 'capture' : 'complete',
        reason: behind ? ARCHIVE_REASON : 'Every required session is archived.',
        earliest_required_session_ms: NOW_MS - 400 * DAY_MS,
        complete_through_session_ms: behind ? NOW_MS - 30 * DAY_MS : NOW_MS - 3 * DAY_MS,
        last_archived_session_ms: NOW_MS - 3 * DAY_MS,
        next_due_ms: NOW_MS + 3_600_000,
        last_success_ms: NOW_MS - 3_600_000,
        pending_sessions: behind ? ARCHIVE_PENDING : 0,
        partial_sessions: behind ? ARCHIVE_PARTIAL : 0,
        lost_sessions: ARCHIVE_LOST,
        snapshot_session_ms: NOW_MS - 3 * DAY_MS,
        snapshot_missed_evenings: 0,
        snapshot_incomplete: 0,
        snapshot_field_gaps: 0,
        last_failure: null,
        gaps: behind
          ? [
              { session_ms: NOW_MS - 29 * DAY_MS, recorded_ms: NOW_MS - 28 * DAY_MS, permanent: false, reason: 'This session has not been fully archived.', days_left: 330 },
              { session_ms: NOW_MS - 28 * DAY_MS, recorded_ms: NOW_MS - 27 * DAY_MS, permanent: false, reason: 'This session has not been fully archived.', days_left: 331 },
            ]
          : [],
      },
      { underlying: 'QQQ', severity: 'healthy', phase: 'complete', reason: 'Every required session is archived.', gaps: [], pending_sessions: 0, partial_sessions: 0, lost_sessions: 0, complete_through_session_ms: NOW_MS - 3 * DAY_MS, last_archived_session_ms: NOW_MS - 3 * DAY_MS },
    ],
    backup: {
      severity: 'healthy',
      reason: 'The latest lake backup is current.',
      last_success_ms: NOW_MS - 2 * 3_600_000,
      oldest_unbacked_capture_ms: null,
      generation_id: 'gen-0001',
      free_bytes: 412_000_000_000,
    },
    test_restore: { severity: 'healthy', reason: 'The weekly restore check passed.', last_success_ms: NOW_MS - 2 * DAY_MS },
  };
}

/** `GET /api/option-archive/status`, read by the Option archive and Backups tabs each time they open. An unreadable archive answers 503. */
export function archiveStatusHandler(archive: ArchiveCondition): Handler {
  return ({ method, path }) => {
    if (method !== 'GET' || path !== '/api/option-archive/status') return null;
    return archive === 'unreadable' ? { status: 503, json: { detail: 'The archive status could not be read.' } } : ok(archiveStatus(archive));
  };
}

const sessionOpenMs = (day: string): number => etMidnightMs(day) + 9.5 * 3_600_000;

/** Every weekday of the window the lake holds, complete: the coverage a symbol's heatmap draws. */
function heldDays(startMs: number, endMs: number): CoverageDay[] {
  const days: CoverageDay[] = [];
  for (let day = FIRST_HELD_DAY; day <= LAST_HELD_DAY; day = addDays(day, 1)) {
    const open = sessionOpenMs(day);
    if (isWeekend(day) || open < startMs || open > endMs) continue;
    days.push({ trading_date_ms: open, status: 'complete', artifact_id: days.length + 1 });
  }
  return days;
}

/** What the Lake tab and the Math checks tab read: every held symbol run-ready over its span, and a few reference fixtures. */
export const operationsTabHandlers: Handler = ({ method, path, query }) => {
  if (method !== 'GET') return null;
  if (path === '/api/data-lake/coverage') {
    const coverage: CoverageResponse = {
      market: 'usa',
      symbol: query.get('symbol') ?? 'SPY',
      data_type: 'trade',
      resolution: 'minute',
      provider: 'polygon',
      price_adjustment_mode: 'polygon_split_adjusted',
      days: heldDays(Number(query.get('start_trading_date_ms')), Number(query.get('end_trading_date_ms'))),
    };
    return ok(coverage);
  }
  if (path === '/api/data-lake/run-readiness') {
    const readiness: RunReadinessResponse = {
      market: 'usa',
      price_adjustment_mode: 'polygon_split_adjusted',
      data_type: 'trade',
      symbols: HELD_SYMBOLS.map((symbol) => ({
        symbol,
        first_trading_date_ms: sessionOpenMs(FIRST_HELD_DAY),
        last_trading_date_ms: sessionOpenMs(LAST_HELD_DAY),
        session_count: 680,
        run_ready_count: 680,
        unrebuildable_count: 0,
        rebuild_first_trading_date_ms: sessionOpenMs(FIRST_HELD_DAY),
        rebuild_last_trading_date_ms: sessionOpenMs(LAST_HELD_DAY),
        gap_session_count: 0,
        gap_count: 0,
        unfillable_gap_session_count: 0,
        fill_first_trading_date_ms: null,
        fill_last_trading_date_ms: null,
      })),
    };
    return ok(readiness);
  }
  if (path === '/api/golden-fixtures') {
    const catalog: GoldenFixturesCatalog = {
      fixtures: [
        {
          id: 'ema-10-lean',
          name: 'EMA 10 against LEAN',
          category: 'indicators',
          canonical_module: 'app.engine.indicators.ema',
          canonical_callable: 'ema',
          reference_kind: 'cross_engine',
          is_certified: true,
          status: 'active',
          active_version: 1,
          tolerance: { atol: 1e-9, rtol: 0, note: 'Strict float.' },
        },
      ],
      validation: { generated_at: '2026-09-20T22:00:00Z', passed: 1, failed: 0, errors: 0, status: 'passed' },
    };
    return ok(catalog);
  }
  return null;
};

/** Both reads of the health world, for a walk that needs only the one condition. */
export function operationsHandlers(archive: ArchiveCondition): Handler[] {
  return [scopedHealthHandler(archive), archiveStatusHandler(archive)];
}
