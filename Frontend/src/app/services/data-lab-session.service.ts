import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, firstValueFrom, map, of, throwError } from 'rxjs';
import type { components } from '../api/broker.types';
import { environment } from '../../environments/environment';
import type {
  BarSources,
  ChartBar,
  QualityReport,
} from '../components/data-lab/data-lab-chart/data-lab-chart.component';
import type { ChartIndicatorResult } from '../shared/trading-chart';
import { failureAsError, isPythonRefusal } from './python-http-failure';

type Schema = components['schemas'];

// ─────────────────────────────────────────────────────────────
// Public interfaces (consumed by components)
// ─────────────────────────────────────────────────────────────

export interface DataLabSessionConfig {
  ticker: string;
  fromDate: string;
  toDate: string;
  session: 'rth' | 'extended';
  forwardFill: boolean;
  adjusted: boolean;
  entries: { name: string; params: Record<string, number> }[];
}

export interface DataLabSessionChartSnapshot {
  timeframe: string;
  bars: ChartBar[];
  indicators: ChartIndicatorResult[];
  quality: QualityReport;
  allowedTimeframes: string[];
  estimatedBarsPerTimeframe: Record<string, number>;
  recommendedTimeframe: string;
  visibleIndicatorIds: string[];
  /** Where the saved bars came from, so a restored chart keeps the provenance
   *  notice it was showing. Optional: snapshots saved before this field existed
   *  simply have no receipt, and restore with none. */
  barSources?: BarSources | null;
  /** The backend's note on indicator values the held history could not warm
   *  up, so a restored chart keeps saying it. Optional for the same reason. */
  warmupNote?: string | null;
}

export interface DataLabSession {
  id: string;
  name: string;
  /** int64 ms UTC, stamped by the server. */
  createdAtMs: number;
  /** int64 ms UTC, stamped by the server. */
  updatedAtMs: number;
  config: DataLabSessionConfig;
  chartSnapshot: DataLabSessionChartSnapshot | null;
}

export interface DataLabSessionSummary {
  id: string;
  name: string;
  /** int64 ms UTC, stamped by the server. */
  createdAtMs: number;
  /** int64 ms UTC, stamped by the server. */
  updatedAtMs: number;
  ticker: string;
  fromDate: string;
  toDate: string;
  indicatorCount: number;
  hasChart: boolean;
}

// ─────────────────────────────────────────────────────────────
// Wire shapes
// ─────────────────────────────────────────────────────────────

/**
 * Python stores `entries` and `chart_snapshot` as opaque JSON documents the
 * screen owns, so the generated schema types them as bare objects. This
 * service is the one place that states what the screen put in them.
 */
interface SessionDocuments {
  entries: DataLabSessionConfig['entries'];
  chart_snapshot: DataLabSessionChartSnapshot | null;
}
type SessionBody = Omit<Schema['DataLabSessionRequest'], keyof SessionDocuments> & SessionDocuments;
type SessionRow = Omit<Schema['DataLabSessionResponse'], keyof SessionDocuments> & SessionDocuments;

/** The `code` Python gives a 404 that names a missing session (and not a missing route). */
const SESSION_NOT_FOUND = 'DATA_LAB_SESSION_NOT_FOUND';

// ─────────────────────────────────────────────────────────────
// Service
// ─────────────────────────────────────────────────────────────

/**
 * Saved Data Lab setups over Python's `/api/data-lab/sessions`.
 *
 * Every failure rejects with an `Error` whose message is Python's sentence
 * (a missing session, an unreachable store, a refused body). The one outcome
 * that resolves instead is `getSession` for an id nobody holds.
 */
@Injectable({ providedIn: 'root' })
export class DataLabSessionService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/data-lab/sessions`;

  /** List all sessions (most recently updated first), returning lightweight summaries:
   *  Python's list carries no entries and no chart snapshot, only `getSession` does. */
  listSessions(): Promise<DataLabSessionSummary[]> {
    return firstValueFrom(
      this.http.get<Schema['DataLabSessionSummary'][]>(this.base).pipe(
        failureAsError('Could not load the saved setups'),
        map(rows => rows.map(toSummary)),
      ),
    );
  }

  /** Get a full session by ID; `undefined` when no such session is held. */
  getSession(id: string): Promise<DataLabSession | undefined> {
    return firstValueFrom(
      this.http.get<SessionRow>(`${this.base}/${id}`).pipe(
        map((row): DataLabSession | undefined => toSession(row)),
        catchError(err => (isPythonRefusal(err, 404, SESSION_NOT_FOUND) ? of(undefined) : throwError(() => err))),
        failureAsError('Could not load the setup'),
      ),
    );
  }

  /** Save a new session. Returns the generated ID. */
  saveSession(
    config: DataLabSessionConfig,
    chartSnapshot: DataLabSessionChartSnapshot | null,
    name?: string,
  ): Promise<string> {
    const autoName = name || `${config.ticker} ${config.fromDate} → ${config.toDate}`;
    return firstValueFrom(
      this.http.post<Schema['DataLabSessionReceipt']>(this.base, toBody(autoName, config, chartSnapshot)).pipe(
        failureAsError('Could not save the setup'),
        map(receipt => receipt.id),
      ),
    );
  }

  /** Update an existing session (full replace). */
  updateSession(
    id: string,
    config: DataLabSessionConfig,
    chartSnapshot: DataLabSessionChartSnapshot | null,
    name: string,
  ): Promise<void> {
    return settled(
      this.http.put<Schema['DataLabSessionReceipt']>(`${this.base}/${id}`, toBody(name, config, chartSnapshot)),
      'Could not update the setup',
    );
  }

  /** Rename a session. */
  renameSession(id: string, newName: string): Promise<void> {
    const body: Schema['DataLabSessionRenameRequest'] = { name: newName };
    return settled(
      this.http.patch<Schema['DataLabSessionReceipt']>(`${this.base}/${id}/name`, body),
      'Could not rename the setup',
    );
  }

  /** Delete a session by ID. */
  deleteSession(id: string): Promise<void> {
    return settled(this.http.delete(`${this.base}/${id}`), 'Could not delete the setup');
  }
}

// ─────────────────────────────────────────────────────────────
// Mapping
// ─────────────────────────────────────────────────────────────

function settled(call: Observable<unknown>, context: string): Promise<void> {
  return firstValueFrom(call.pipe(failureAsError(context), map(() => undefined)));
}

function toBody(
  name: string,
  config: DataLabSessionConfig,
  chartSnapshot: DataLabSessionChartSnapshot | null,
): SessionBody {
  return {
    name,
    ticker: config.ticker,
    from_date: config.fromDate,
    to_date: config.toDate,
    session: config.session,
    forward_fill: config.forwardFill,
    adjusted: config.adjusted,
    entries: config.entries,
    chart_snapshot: chartSnapshot,
  };
}

function toSummary(row: Schema['DataLabSessionSummary']): DataLabSessionSummary {
  return {
    id: row.id,
    name: row.name,
    createdAtMs: row.created_at_ms,
    updatedAtMs: row.updated_at_ms,
    ticker: row.ticker,
    fromDate: row.from_date,
    toDate: row.to_date,
    indicatorCount: row.entry_count,
    hasChart: row.has_chart,
  };
}

function toSession(row: SessionRow): DataLabSession {
  return {
    id: row.id,
    name: row.name,
    createdAtMs: row.created_at_ms,
    updatedAtMs: row.updated_at_ms,
    config: {
      ticker: row.ticker,
      fromDate: row.from_date,
      toDate: row.to_date,
      session: row.session === 'extended' ? 'extended' : 'rth',
      forwardFill: row.forward_fill,
      adjusted: row.adjusted,
      entries: row.entries,
    },
    chartSnapshot: row.chart_snapshot,
  };
}
