import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import { tradingDateToMs } from './trading-range';
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
  /**
   * `YYYY-MM-DD` — what `<input type="date">` produces and what the operator
   * reads. The wire takes `int64 ms UTC`; `coverage()` converts at the HTTP
   * seam via `tradingDateToMs`, so the ISO form never leaves the browser.
   */
  readonly startTradingDate: string;
  readonly endTradingDate: string;
  readonly market?: string;
  readonly dataType?: DataLakeDataType;
  /**
   * No default (#1890): the data plane used to fall back to `"raw"` when a
   * caller omitted this, and for most of the lake's life the raw root held
   * zero catalogued rows even when `polygon_split_adjusted` was fully
   * populated for a symbol — an unqualified request silently read
   * "missing" for data that actually existed under the other root. The
   * endpoint now 422s on an omitted value instead, so this is required
   * here too rather than papered over with a client-side fallback.
   */
  readonly priceAdjustmentMode: PriceAdjustmentMode;
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
    const startMs = tradingDateToMs(query.startTradingDate);
    const endMs = tradingDateToMs(query.endTradingDate);
    if (startMs === null || endMs === null) {
      // Named locally rather than sent. A fallback anchor here (epoch 0, say)
      // would turn an unparseable date into a *valid* request for a window in
      // 1970 -- the endpoint would answer it, and the operator would read a
      // successful empty heatmap as "the lake holds nothing" instead of "that
      // date does not parse".
      return {
        kind: 'rejected',
        reason: 'invalid_trading_date',
        message: 'Pick a start and end date in YYYY-MM-DD form.',
      };
    }
    return this.read(() =>
      firstValueFrom(
        this.http.get<CoverageResponse>(`${this.base}/coverage`, {
          params: {
            symbol: query.symbol,
            start_trading_date_ms: startMs,
            end_trading_date_ms: endMs,
            market: query.market ?? 'usa',
            data_type: query.dataType ?? 'trade',
            price_adjustment_mode: query.priceAdjustmentMode,
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
 * The typed body is consulted before the status code. This used to separate
 * two different 404s: the catalog having no such artifact
 * (`{reason: "artifact_not_found"}`) versus `main.py` never mounting the
 * router because `DATA_LAKE_ENABLED` was off. #1893 retired the flag and the
 * router is always mounted, so the only 404 this surface produces is the
 * typed one, which renders its reason through the receipt-label pipe. An
 * untyped 404 is now genuinely unexpected and folds into `unavailable`
 * rather than claiming the lake is switched off.
 */
export function classifyDataLakeError(error: unknown): DataLakeFailure {
  if (!(error instanceof HttpErrorResponse)) {
    return { kind: 'unavailable', message: error instanceof Error ? error.message : String(error) };
  }
  const problem = typedProblem(error);
  if (problem !== null) return { kind: 'rejected', ...problem };
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
 */
export function describeFailure(read: DataLakeRead<unknown> | undefined): DataLakeProblem | null {
  if (read === undefined || read.kind === 'ok') return null;
  if (read.kind === 'rejected') return { reason: read.reason, message: read.message };
  return { reason: 'unavailable', message: read.message };
}
