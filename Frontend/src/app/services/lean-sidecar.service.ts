import { HttpClient, HttpErrorResponse } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { firstValueFrom } from "rxjs";

import { environment } from "../../environments/environment";
import type {
  LeanSidecarErrorEnvelope,
  LeanLauncherDiagnosticReport,
} from "./lean-sidecar.types";

/**
 * Frontend client for the LEAN Sidecar Lab data-plane endpoints.
 *
 * Calls REST directly against the polygon-data-service container via
 * the Angular proxy (``/api`` → ``python-service:8000``). The service
 * extracts the launcher's stable ``{reason, message}`` envelope into a
 * ``LeanSidecarApiError`` so the component can branch on the reason
 * label without parsing the raw ``HttpErrorResponse``.
 */

export class LeanSidecarApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly reason: string,
    message: string,
  ) {
    super(message);
    this.name = "LeanSidecarApiError";
  }
}

@Injectable({ providedIn: "root" })
export class LeanSidecarService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/lean-sidecar`;

  async diagnose(): Promise<LeanLauncherDiagnosticReport> {
    try {
      return await firstValueFrom(
        this.http.get<LeanLauncherDiagnosticReport>(`${this.base}/diagnose`),
      );
    } catch (err) {
      throw this.translate(err);
    }
  }

  /**
   * Resolve the next NYSE trading session strictly after ``date`` to
   * its 09:30 ET session-open as int64 ms UTC. The unified Engine Lab
   * uses this to advance the operator's chosen end date to the
   * half-open window's exclusive ``end_ms_utc`` (a trading date anchored
   * at its session open, ADR 0022 (a)).
   *
   * Server-side delegation keeps the NYSE calendar (weekends, holidays,
   * MLK / Thanksgiving / Good-Friday skips) in one place — the
   * ``app/lean_sidecar/trading_calendar.py`` module — instead of
   * reproducing it in TypeScript and risking drift with the validator.
   */
  async nextTradingDayOpen(
    isoDate: string,
  ): Promise<{ next_trading_date: string; session_open_ms_utc: number }> {
    try {
      return await firstValueFrom(
        this.http.get<{ next_trading_date: string; session_open_ms_utc: number }>(
          `${this.base}/calendar/next-trading-day-open`,
          { params: { date: isoDate } },
        ),
      );
    } catch (err) {
      throw this.translate(err);
    }
  }

  /**
   * Convert an ``HttpErrorResponse`` into a ``LeanSidecarApiError``
   * carrying the launcher's stable reason label. Falls back to
   * ``"unknown"`` when the body doesn't match the documented
   * ``{detail: {reason, message}}`` envelope.
   */
  private translate(err: unknown): LeanSidecarApiError {
    if (err instanceof HttpErrorResponse) {
      const detail = err.error?.detail;
      if (detail && typeof detail === "object" && typeof detail.reason === "string") {
        const envelope = detail as LeanSidecarErrorEnvelope;
        return new LeanSidecarApiError(err.status, envelope.reason, envelope.message);
      }
      if (err.status === 422) {
        return new LeanSidecarApiError(
          422,
          "validation_error",
          typeof err.error === "string" ? err.error : JSON.stringify(err.error),
        );
      }
      return new LeanSidecarApiError(
        err.status,
        "unknown",
        err.message || `HTTP ${err.status}`,
      );
    }
    return new LeanSidecarApiError(
      0,
      "unknown",
      err instanceof Error ? err.message : String(err),
    );
  }
}
