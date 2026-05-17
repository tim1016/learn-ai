import { HttpClient, HttpErrorResponse } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { firstValueFrom } from "rxjs";
import { environment } from "../../environments/environment";
import type {
  LeanSidecarErrorEnvelope,
  NormalizedResult,
  TrustedRunRequest,
  TrustedRunResponse,
} from "./lean-sidecar.types";

/**
 * Frontend client for the LEAN Sidecar Lab data-plane endpoints.
 *
 * Calls REST directly against the polygon-data-service container via
 * the Angular proxy (`/api` → `python-service:8000`). The service
 * extracts the launcher's stable `{reason, message}` envelope into a
 * `LeanSidecarApiError` so the component can branch on the reason
 * label without parsing the raw HttpErrorResponse.
 *
 * Phase 4a — read-only HTTP client. Phase 4b will likely add a thin
 * Backend GraphQL passthrough so the .NET layer can wrap auth/logging,
 * mirroring the spec-strategy passthrough pattern.
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

  async startTrustedRun(request: TrustedRunRequest): Promise<TrustedRunResponse> {
    try {
      return await firstValueFrom(
        this.http.post<TrustedRunResponse>(`${this.base}/trusted-runs`, request),
      );
    } catch (err) {
      throw this.translate(err);
    }
  }

  async getNormalized(runId: string): Promise<NormalizedResult> {
    try {
      return await firstValueFrom(
        this.http.get<NormalizedResult>(`${this.base}/runs/${encodeURIComponent(runId)}/normalized`),
      );
    } catch (err) {
      throw this.translate(err);
    }
  }

  async getLogTail(runId: string): Promise<string> {
    try {
      return await firstValueFrom(
        this.http.get(`${this.base}/runs/${encodeURIComponent(runId)}/log`, {
          responseType: "text",
        }),
      );
    } catch (err) {
      throw this.translate(err);
    }
  }

  async getObservationsCsv(runId: string): Promise<string> {
    try {
      return await firstValueFrom(
        this.http.get(`${this.base}/runs/${encodeURIComponent(runId)}/observations`, {
          responseType: "text",
        }),
      );
    } catch (err) {
      throw this.translate(err);
    }
  }

  /**
   * Convert an HttpErrorResponse into a LeanSidecarApiError carrying the
   * launcher's stable reason label. Falls back to "unknown" when the
   * body doesn't match the documented `{detail: {reason, message}}`
   * envelope — operators still see the raw status + text.
   */
  private translate(err: unknown): LeanSidecarApiError {
    if (err instanceof HttpErrorResponse) {
      const detail = err.error?.detail;
      if (detail && typeof detail === "object" && typeof detail.reason === "string") {
        const envelope = detail as LeanSidecarErrorEnvelope;
        return new LeanSidecarApiError(err.status, envelope.reason, envelope.message);
      }
      // Pydantic 422 body has a `detail` array; surface it as reason="validation_error"
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
