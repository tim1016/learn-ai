import { HttpClient, HttpErrorResponse, HttpParams } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { catchError, Observable, of, throwError } from "rxjs";

import { environment } from "../../environments/environment";
import type { BacktestRunDetail, BacktestRunSummary, Engine } from "./backtest-runs.types";

/** The history table requests one fixed page and never pages. */
export const HISTORY_PAGE_SIZE = 50;

/**
 * Backtest-run reads and verbs over FastAPI (PRD #1929): the history list,
 * the run report, and the researcher's notes. Python owns the rows; this
 * service owns the transport and nothing else, so components mock a
 * service rather than a wire.
 */
@Injectable({ providedIn: "root" })
export class BacktestRunsService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/research/backtest-runs`;

  /** Run history newest-first, optionally one engine's. */
  list(engine: Engine | null, limit = HISTORY_PAGE_SIZE): Observable<BacktestRunSummary[]> {
    let params = new HttpParams({ fromObject: { limit: String(limit) } });
    if (engine !== null) params = params.set("engine", engine);
    return this.http.get<BacktestRunSummary[]>(this.base, { params });
  }

  /** One run's report, or `null` when the server does not have it. */
  get(id: number): Observable<BacktestRunDetail | null> {
    return this.http.get<BacktestRunDetail>(`${this.base}/${id}`).pipe(
      catchError((error: unknown) =>
        error instanceof HttpErrorResponse && error.status === 404 ? of(null) : throwError(() => error),
      ),
    );
  }

  updateNotes(id: number, notes: string): Observable<{ id: number; notes: string | null }> {
    return this.http.patch<{ id: number; notes: string | null }>(`${this.base}/${id}/notes`, { notes });
  }
}
