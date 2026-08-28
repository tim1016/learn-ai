import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import type {
  ArtifactDetail,
  BackfillDefaults,
  CoverageResponse,
  DataLakeDataType,
  DataLakeFailure,
  DataLakeRead,
  PriceAdjustmentMode,
  StorageSummaryResponse,
} from './data-lake.types';

export interface CoverageQuery {
  readonly symbol: string;
  /** `YYYY-MM-DD`, the shape the coverage endpoint's `date` query params take. */
  readonly startTradingDate: string;
  readonly endTradingDate: string;
  readonly market?: string;
  readonly dataType?: DataLakeDataType;
  readonly priceAdjustmentMode?: PriceAdjustmentMode;
}

/** The `{reason, message}` object every typed data-lake rejection puts in `detail`. */
export interface DataLakeProblem {
  readonly reason: string;
  readonly message: string;
}

/**
 * Reads the flag-gated data-lake surface directly from the Python data
 * plane (`/api/data-lake/*` proxies straight through; only `/api/jobs` is
 * served by .NET).
 *
 * Every call resolves to a `DataLakeRead` rather than throwing, so the
 * "lake is dark" case (`DATA_LAKE_ENABLED` off → 404 on every route) is a
 * named outcome the page renders honestly instead of an unclassified error.
 */
@Injectable({ providedIn: 'root' })
export class DataLakeService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/data-lake`;

  async coverage(query: CoverageQuery): Promise<DataLakeRead<CoverageResponse>> {
    return this.read(() =>
      firstValueFrom(
        this.http.get<CoverageResponse>(`${this.base}/coverage`, {
          params: {
            symbol: query.symbol,
            start_trading_date: query.startTradingDate,
            end_trading_date: query.endTradingDate,
            market: query.market ?? 'usa',
            data_type: query.dataType ?? 'trade',
            price_adjustment_mode: query.priceAdjustmentMode ?? 'raw',
          },
        }),
      ),
    );
  }

  async artifact(artifactId: number): Promise<DataLakeRead<ArtifactDetail>> {
    return this.read(() =>
      firstValueFrom(this.http.get<ArtifactDetail>(`${this.base}/artifacts/${artifactId}`)),
    );
  }

  async storageSummary(market = 'usa'): Promise<DataLakeRead<StorageSummaryResponse>> {
    return this.read(() =>
      firstValueFrom(
        this.http.get<StorageSummaryResponse>(`${this.base}/storage-summary`, {
          params: { market },
        }),
      ),
    );
  }

  async backfillDefaults(market = 'usa'): Promise<DataLakeRead<BackfillDefaults>> {
    return this.read(() =>
      firstValueFrom(
        this.http.get<BackfillDefaults>(`${this.base}/backfill-defaults`, { params: { market } }),
      ),
    );
  }

  private async read<T>(request: () => Promise<T>): Promise<DataLakeRead<T>> {
    try {
      return { kind: 'ok', value: await request() };
    } catch (error) {
      return classifyDataLakeError(error);
    }
  }
}

/**
 * Reads the router's own typed rejection body, if it sent one.
 *
 * Every deliberate refusal on this surface raises
 * `HTTPException(detail={"reason": ..., "message": ...})` — the 422
 * validators on `/coverage` and the `artifact_not_found` 404 on
 * `/artifacts/{id}` alike. FastAPI's own 404 (the route is not mounted)
 * carries the bare string `"Not Found"` instead, which is exactly what
 * makes the two 404s tellable apart.
 */
function typedProblem(error: HttpErrorResponse): DataLakeProblem | null {
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  if (typeof detail !== 'object' || detail === null) return null;
  const { reason, message } = detail as { reason?: unknown; message?: unknown };
  if (typeof reason !== 'string') return null;
  return { reason, message: typeof message === 'string' ? message : error.message };
}

/**
 * Maps a failed data-lake request onto its named outcome.
 *
 * The typed body is consulted before the status code, because the status
 * alone cannot separate the two 404s this surface produces: one means the
 * catalog has no such artifact (`{reason: "artifact_not_found"}`), the
 * other means `main.py` never mounted the router because
 * `DATA_LAKE_ENABLED` is off. Only a 404 with no typed body is the dark
 * lake; a typed one is a rejection like any other and renders its reason
 * through the receipt-label pipe.
 */
export function classifyDataLakeError(error: unknown): DataLakeFailure {
  if (!(error instanceof HttpErrorResponse)) {
    return { kind: 'unavailable', message: error instanceof Error ? error.message : String(error) };
  }
  const problem = typedProblem(error);
  if (problem !== null) return { kind: 'rejected', ...problem };
  if (error.status === 404) return { kind: 'not_enabled' };
  if (error.status === 422) {
    return { kind: 'rejected', reason: 'validation_failed', message: error.message };
  }
  return {
    kind: 'unavailable',
    message: error.status === 0 ? 'The data plane did not respond.' : error.message,
  };
}

/**
 * The one fold from a read to the `{reason, message}` pair every panel
 * renders — reason through the receipt-label pipe, message as the
 * backend's own words.
 *
 * `not_enabled` gets a reason code of its own rather than a null, so a
 * surface that only knows how to render a problem still says something
 * true instead of going blank. A surface that treats the dark lake
 * specially — the Observatory page, which shows a page-wide banner —
 * checks the kind directly and never reaches this.
 */
export function describeFailure(read: DataLakeRead<unknown> | undefined): DataLakeProblem | null {
  if (read === undefined || read.kind === 'ok') return null;
  if (read.kind === 'rejected') return { reason: read.reason, message: read.message };
  if (read.kind === 'unavailable') return { reason: 'unavailable', message: read.message };
  return {
    reason: 'data_lake_not_enabled',
    message: 'The data lake is not enabled on this data plane.',
  };
}
