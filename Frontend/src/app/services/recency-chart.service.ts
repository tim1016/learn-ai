import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom, map, type Observable } from 'rxjs';

import { environment } from '../../environments/environment';
import { JobsService } from './jobs.service';

/** A trade as the chart consumes it — the shape the retired GraphQL query served, kept so the swimlane is untouched. */
export interface RecencyTrade {
  symbol: string;
  strategyKey: string;
  paramsHash: string;
  paramsJson: string;
  fingerprint: string;
  entryMs: number;
  exitMs: number;
  pnlPts: number;
  pnlPct: number;
  quantity: number;
  pnl: number;
  holdingSessions: number;
  sharpe: number | null;
  studyId: number | null;
  recencyRunId: number;
  isSyntheticExit: boolean;
  signalReason: string;
  memberships: RecencyTradeMembership[];
}

export interface RecencyTradeMembership {
  recencyRunId: number;
  studyId: number | null;
  createdAtMs: number;
}

export interface RecencyHero {
  symbol: string;
  strategyKey: string;
  paramsHash: string;
  totalPnl: number;
  recencyRunId: number;
}

export interface RecencyWindowQuery {
  fromMs: number;
  toMs: number;
  symbols?: readonly string[];
  strategies?: readonly string[];
}

/**
 * One launch as the launches list serves it (#1938). `status` is presented —
 * a stored running launch whose worker is gone reads `interrupted`; `request`
 * is the stored configuration, resent verbatim (plus `resume_launch_id`) on
 * a resume.
 */
export interface RecencyLaunch {
  launchId: string;
  status: string;
  attempt: number;
  expectedRuns: number;
  succeededRuns: number;
  failedRuns: number;
  createdAtMs: number;
  completedAtMs: number | null;
  resumable: boolean;
  resumeRefusal: string | null;
  request: Record<string, unknown>;
}

interface RecencyLaunchDto {
  launch_id: string;
  status: string;
  attempt: number;
  expected_runs: number;
  succeeded_runs: number;
  failed_runs: number;
  created_at_ms: number;
  completed_at_ms: number | null;
  resumable: boolean;
  resume_refusal: string | null;
  request: Record<string, unknown>;
}

function toLaunch(dto: RecencyLaunchDto): RecencyLaunch {
  return {
    launchId: dto.launch_id,
    status: dto.status,
    attempt: dto.attempt,
    expectedRuns: dto.expected_runs,
    succeededRuns: dto.succeeded_runs,
    failedRuns: dto.failed_runs,
    createdAtMs: dto.created_at_ms,
    completedAtMs: dto.completed_at_ms,
    resumable: dto.resumable,
    resumeRefusal: dto.resume_refusal,
    request: dto.request,
  };
}

/** Wire shapes of `/api/research/recency` (snake_case, numbers as JSON numbers). */
interface RecencyTradeDto {
  symbol: string;
  strategy_key: string;
  params_hash: string;
  params_json: string;
  fingerprint: string;
  entry_ms: number;
  exit_ms: number;
  pnl_pts: number;
  pnl_pct: number;
  quantity: number;
  pnl: number;
  holding_sessions: number;
  sharpe: number | null;
  study_id: number | null;
  recency_run_id: number;
  is_synthetic_exit: boolean;
  signal_reason: string;
  memberships: { recency_run_id: number; study_id: number | null; created_at_ms: number }[];
}

interface RecencyHeroDto {
  heroes: { recency_run_id: number; symbol: string; strategy_key: string; params_hash: string; total_pnl: number }[];
}

/** The explicit boundary adapter: the wire DTO is Python's; the chart's model is unchanged. */
function toTrade(dto: RecencyTradeDto): RecencyTrade {
  return {
    symbol: dto.symbol,
    strategyKey: dto.strategy_key,
    paramsHash: dto.params_hash,
    paramsJson: dto.params_json,
    fingerprint: dto.fingerprint,
    entryMs: dto.entry_ms,
    exitMs: dto.exit_ms,
    pnlPts: dto.pnl_pts,
    pnlPct: dto.pnl_pct,
    quantity: dto.quantity,
    pnl: dto.pnl,
    holdingSessions: dto.holding_sessions,
    sharpe: dto.sharpe,
    studyId: dto.study_id,
    recencyRunId: dto.recency_run_id,
    isSyntheticExit: dto.is_synthetic_exit,
    signalReason: dto.signal_reason,
    memberships: dto.memberships.map((m) => ({ recencyRunId: m.recency_run_id, studyId: m.study_id, createdAtMs: m.created_at_ms })),
  };
}

function windowParams(query: RecencyWindowQuery): HttpParams {
  let params = new HttpParams({ fromObject: { from_ms: String(query.fromMs), to_ms: String(query.toMs) } });
  for (const symbol of query.symbols ?? []) params = params.append('symbols', symbol);
  for (const strategy of query.strategies ?? []) params = params.append('strategies', strategy);
  return params;
}

/**
 * Recency Chart reads and mutations over FastAPI (PRD #1927): the trades the
 * chart draws, the visible-window heroes, and soft-delete of a run. Python
 * owns the rows; this service owns the wire-to-model adapter and nothing else.
 * The reads are Observables because the page feeds them to `rxResource`; the
 * verb is a Promise like the sibling services' one-shot calls.
 */
@Injectable({ providedIn: 'root' })
export class RecencyChartService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);
  private readonly base = `${environment.pythonServiceUrl}/api/research/recency`;

  trades(query: RecencyWindowQuery): Observable<RecencyTrade[]> {
    return this.http.get<RecencyTradeDto[]>(`${this.base}/trades`, { params: windowParams(query) }).pipe(map((dtos) => dtos.map(toTrade)));
  }

  heroes(query: RecencyWindowQuery): Observable<RecencyHero[]> {
    return this.http.get<RecencyHeroDto>(`${this.base}/hero`, { params: windowParams(query) }).pipe(
      map((dto) =>
        dto.heroes.map((h) => ({ symbol: h.symbol, strategyKey: h.strategy_key, paramsHash: h.params_hash, totalPnl: h.total_pnl, recencyRunId: h.recency_run_id })),
      ),
    );
  }

  launches(limit = 20): Observable<RecencyLaunch[]> {
    const params = new HttpParams({ fromObject: { limit: String(limit) } });
    return this.http.get<RecencyLaunchDto[]>(`${this.base}/launches`, { params }).pipe(map((dtos) => dtos.map(toLaunch)));
  }

  /** Continues an interrupted, failed or cancelled launch under a new job; the stored configuration governs. */
  async resume(launch: RecencyLaunch): Promise<string> {
    return this.jobs.startJob('recency_chart', { ...launch.request, resume_launch_id: launch.launchId });
  }

  async softDeleteRun(recencyRunId: number): Promise<void> {
    await firstValueFrom(this.http.post(`${this.base}/runs/${recencyRunId}/soft-delete`, {}));
  }
}
