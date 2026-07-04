/* eslint-disable @typescript-eslint/no-explicit-any */
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';

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
  bars: any[];
  indicators: any[];
  quality: any;
  allowedTimeframes: string[];
  estimatedBarsPerTimeframe: Record<string, number>;
  recommendedTimeframe: string;
  visibleIndicatorIds: string[];
}

export interface DataLabSession {
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
  config: DataLabSessionConfig;
  chartSnapshot: DataLabSessionChartSnapshot | null;
}

export interface DataLabSessionSummary {
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
  ticker: string;
  fromDate: string;
  toDate: string;
  indicatorCount: number;
  hasChart: boolean;
}

// ─────────────────────────────────────────────────────────────
// GraphQL queries & mutations
// ─────────────────────────────────────────────────────────────

const LIST_SESSIONS = `
  query ListDataLabSessions {
    dataLabSessions {
      id
      name
      createdAt
      updatedAt
      ticker
      fromDate
      toDate
      session
      forwardFill
      adjusted
      entriesJson
      chartSnapshotJson
    }
  }
`;

const GET_SESSION = `
  query GetDataLabSession($id: UUID!) {
    dataLabSession(id: $id) {
      id
      name
      createdAt
      updatedAt
      ticker
      fromDate
      toDate
      session
      forwardFill
      adjusted
      entriesJson
      chartSnapshotJson
    }
  }
`;

const SAVE_SESSION = `
  mutation SaveDataLabSession($input: DataLabSessionInputInput!) {
    saveDataLabSession(input: $input) {
      success
      id
      message
    }
  }
`;

const UPDATE_SESSION = `
  mutation UpdateDataLabSession($id: UUID!, $input: DataLabSessionInputInput!) {
    updateDataLabSession(id: $id, input: $input) {
      success
      id
      message
    }
  }
`;

const UPDATE_CHART_SNAPSHOT = `
  mutation UpdateDataLabChartSnapshot($id: UUID!, $chartSnapshotJson: String!) {
    updateDataLabChartSnapshot(id: $id, chartSnapshotJson: $chartSnapshotJson) {
      success
      id
      message
    }
  }
`;

const RENAME_SESSION = `
  mutation RenameDataLabSession($id: UUID!, $name: String!) {
    renameDataLabSession(id: $id, name: $name) {
      success
      id
      message
    }
  }
`;

const DELETE_SESSION = `
  mutation DeleteDataLabSession($id: UUID!) {
    deleteDataLabSession(id: $id) {
      success
      id
      message
    }
  }
`;

// ─────────────────────────────────────────────────────────────
// Internal response shapes
// ─────────────────────────────────────────────────────────────

interface RawSession {
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
  ticker: string;
  fromDate: string;
  toDate: string;
  session: string;
  forwardFill: boolean;
  adjusted: boolean;
  entriesJson: string;
  chartSnapshotJson: string | null;
}

interface MutationResult {
  success: boolean;
  id: string | null;
  message: string;
}

interface GqlResponse<T> {
  data: T;
  errors?: { message: string }[];
}

// ─────────────────────────────────────────────────────────────
// Service
// ─────────────────────────────────────────────────────────────

@Injectable({ providedIn: 'root' })
export class DataLabSessionService {
  private http = inject(HttpClient);
  private url = environment.backendUrl;

  // ── Queries ───────────────────────────────────────────────

  /** List all sessions (most recent first), returning lightweight summaries. */
  async listSessions(): Promise<DataLabSessionSummary[]> {
    const resp = await this.gql<{ dataLabSessions: RawSession[] }>(
      LIST_SESSIONS
    );

    return (resp.dataLabSessions ?? []).map(s => {
      const entries = this.parseJson<any[]>(s.entriesJson, []);
      return {
        id: s.id,
        name: s.name,
        createdAt: s.createdAt,
        updatedAt: s.updatedAt,
        ticker: s.ticker,
        fromDate: s.fromDate,
        toDate: s.toDate,
        indicatorCount: entries.length,
        hasChart: s.chartSnapshotJson !== null,
      };
    });
  }

  /** Get a full session by ID (includes parsed chart snapshot). */
  async getSession(id: string): Promise<DataLabSession | undefined> {
    const resp = await this.gql<{ dataLabSession: RawSession | null }>(
      GET_SESSION,
      { id }
    );

    const raw = resp.dataLabSession;
    if (!raw) return undefined;
    return this.toSession(raw);
  }

  // ── Mutations ─────────────────────────────────────────────

  /** Save a new session. Returns the generated ID. */
  async saveSession(
    config: DataLabSessionConfig,
    chartSnapshot: DataLabSessionChartSnapshot | null,
    name?: string
  ): Promise<string | null> {
    const autoName = name || `${config.ticker} ${config.fromDate} → ${config.toDate}`;
    const input = this.toInput(autoName, config, chartSnapshot);

    const resp = await this.gql<{ saveDataLabSession: MutationResult }>(
      SAVE_SESSION,
      { input }
    );
    return resp.saveDataLabSession.success ? resp.saveDataLabSession.id : null;
  }

  /** Update an existing session (full replace). */
  async updateSession(
    id: string,
    config: DataLabSessionConfig,
    chartSnapshot: DataLabSessionChartSnapshot | null,
    name: string
  ): Promise<boolean> {
    const input = this.toInput(name, config, chartSnapshot);

    const resp = await this.gql<{ updateDataLabSession: MutationResult }>(
      UPDATE_SESSION,
      { id, input }
    );
    return resp.updateDataLabSession.success;
  }

  /** Update only the chart snapshot on an existing session. */
  async updateChartSnapshot(
    id: string,
    snapshot: DataLabSessionChartSnapshot
  ): Promise<boolean> {
    const resp = await this.gql<{ updateDataLabChartSnapshot: MutationResult }>(
      UPDATE_CHART_SNAPSHOT,
      { id, chartSnapshotJson: JSON.stringify(snapshot) }
    );
    return resp.updateDataLabChartSnapshot.success;
  }

  /** Rename a session. */
  async renameSession(id: string, newName: string): Promise<boolean> {
    const resp = await this.gql<{ renameDataLabSession: MutationResult }>(
      RENAME_SESSION,
      { id, name: newName }
    );
    return resp.renameDataLabSession.success;
  }

  /** Delete a session by ID. */
  async deleteSession(id: string): Promise<boolean> {
    const resp = await this.gql<{ deleteDataLabSession: MutationResult }>(
      DELETE_SESSION,
      { id }
    );
    return resp.deleteDataLabSession.success;
  }

  // ── Helpers ───────────────────────────────────────────────

  private toInput(
    name: string,
    config: DataLabSessionConfig,
    chartSnapshot: DataLabSessionChartSnapshot | null
  ) {
    return {
      name,
      ticker: config.ticker,
      fromDate: config.fromDate,
      toDate: config.toDate,
      session: config.session,
      forwardFill: config.forwardFill,
      adjusted: config.adjusted,
      entriesJson: JSON.stringify(config.entries),
      chartSnapshotJson: chartSnapshot ? JSON.stringify(chartSnapshot) : null,
    };
  }

  private toSession(raw: RawSession): DataLabSession {
    const entries = this.parseJson<{ name: string; params: Record<string, number> }[]>(
      raw.entriesJson, []
    );
    const chartSnapshot = raw.chartSnapshotJson
      ? this.parseJson<DataLabSessionChartSnapshot | null>(raw.chartSnapshotJson, null)
      : null;

    return {
      id: raw.id,
      name: raw.name,
      createdAt: raw.createdAt,
      updatedAt: raw.updatedAt,
      config: {
        ticker: raw.ticker,
        fromDate: raw.fromDate,
        toDate: raw.toDate,
        session: raw.session as 'rth' | 'extended',
        forwardFill: raw.forwardFill,
        adjusted: raw.adjusted ?? true,
        entries,
      },
      chartSnapshot,
    };
  }

  private parseJson<T>(raw: string | null, fallback: T): T {
    if (!raw) return fallback;
    try { return JSON.parse(raw); }
    catch { return fallback; }
  }

  private async gql<T>(query: string, variables?: Record<string, any>): Promise<T> {
    const resp = await firstValueFrom(
      this.http.post<GqlResponse<T>>(this.url, { query, variables })
    );
    if (resp.errors?.length) {
      throw new Error(resp.errors[0].message);
    }
    return resp.data;
  }
}
